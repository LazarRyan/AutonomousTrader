"""
Adaptive signal blend weights -- the weekly retune.

The blend used to be hard-coded 25/25/25/25 (BlendConfig defaults): a
signal source could be consistently wrong for a month and keep exactly as
much say as one that was consistently right. This module closes that loop:

  1. Every cycle already persists each source's raw score per symbol to
     the `signals` table (src.db.write_signal).
  2. Weekly (Monday's nightly job -- see src/nightly.py), this module
     joins those historical scores against what the price ACTUALLY did
     over the following 5 trading days (forward return, via the same
     Alpaca daily-closes fetch the momentum signal uses).
  3. Per source: hit rate = fraction of meaningful signals (|score| >= 10)
     whose direction matched the forward return's direction. |score| < 10
     is excluded on purpose -- a source saying "roughly neutral" and the
     price wobbling either way is neither a hit nor a miss, and counting
     it would drag every source toward a coin flip.
  4. Weights: each source's edge over a coin flip (hit_rate - 0.5, floored
     at 0) is normalized across sources, then squeezed so every source
     keeps at least WEIGHT_FLOOR (0.10). The floor is load-bearing: a
     source at 0% would never influence a blended score again, so it could
     never produce the very evidence that would rehabilitate it. Sources
     with fewer than MIN_SAMPLES scored outcomes keep a neutral 0.5 hit
     rate ("no evidence" must not mean "condemned" -- congressional
     signals are legitimately sparse).
  5. The result is persisted to config.signal_blend_weights (read by every
     subsequent run_cycle via BlendConfig.from_weights), the evidence is
     written to the vault Scorecard.md (read into every portfolio manager
     prompt by recall), and the change gets an audit_log row.

If NO source has any edge (all hit rates <= 0.5), weights revert to equal
-- "everything's been noise lately" is a real finding, and equal weights
is the honest response to it, not whatever last week's weights were.

Split, as always:
  1. PURE, unit-tested (tests/test_weight_tuner.py): compute_forward_return,
     compute_source_hit_rates, compute_adaptive_weights, render_scorecard.
  2. Thin glue: retune_weights() -- Supabase reads/writes, Alpaca closes
     fetch, vault write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SOURCES = ("momentum", "insider", "congressional", "news_sentiment")

LOOKBACK_DAYS = 30          # how much signal history feeds each retune
FORWARD_HORIZON_BARS = 5    # forward return measured over 5 trading days
MEANINGFUL_SCORE = 10.0     # |score| below this is neutral -- neither hit nor miss
MIN_SAMPLES = 8             # fewer scored outcomes than this = neutral 0.5 hit rate
WEIGHT_FLOOR = 0.10         # no source ever drops below this (see module docstring)


@dataclass(frozen=True)
class SignalOutcome:
    """One historical signal joined against its forward return."""

    source: str
    symbol: str
    score: float
    forward_return: float  # fraction, e.g. +0.023 over the horizon


@dataclass(frozen=True)
class SourceAccuracy:
    source: str
    samples: int          # scored (meaningful) outcomes only
    hits: int
    hit_rate: float       # hits/samples, or 0.5 when samples < MIN_SAMPLES
    defaulted: bool       # True when hit_rate is the no-evidence 0.5 default


def compute_forward_return(closes: list[float], signal_index: int, horizon: int = FORWARD_HORIZON_BARS) -> float | None:
    """Forward return from the close at signal_index to the close `horizon`
    bars later. None when there aren't enough bars yet (a signal from two
    days ago has no 5-day outcome -- it's not scoreable this week, and
    guessing with a shorter horizon would make recent signals systematically
    noisier than older ones). Pure, unit-tested."""
    end = signal_index + horizon
    if signal_index < 0 or end >= len(closes):
        return None
    start_price = closes[signal_index]
    if start_price <= 0:
        return None
    return (closes[end] - start_price) / start_price


def compute_source_hit_rates(outcomes: list[SignalOutcome]) -> dict[str, SourceAccuracy]:
    """Per-source directional hit rate over meaningful signals. Every
    source in SOURCES appears in the result (with the defaulted 0.5 when
    thin) so downstream weight math never has to special-case a missing
    key. Pure, unit-tested."""
    scored: dict[str, list[bool]] = {source: [] for source in SOURCES}
    for outcome in outcomes:
        if outcome.source not in scored:
            continue  # unknown source string in the table -- ignore, don't crash the retune
        if abs(outcome.score) < MEANINGFUL_SCORE:
            continue
        if outcome.forward_return == 0.0:
            continue  # dead-flat forward return: direction undefined
        hit = (outcome.score > 0) == (outcome.forward_return > 0)
        scored[outcome.source].append(hit)

    result: dict[str, SourceAccuracy] = {}
    for source, hits_list in scored.items():
        samples = len(hits_list)
        hits = sum(hits_list)
        if samples < MIN_SAMPLES:
            result[source] = SourceAccuracy(source=source, samples=samples, hits=hits, hit_rate=0.5, defaulted=True)
        else:
            result[source] = SourceAccuracy(
                source=source, samples=samples, hits=hits, hit_rate=hits / samples, defaulted=False
            )
    return result


def compute_adaptive_weights(accuracies: dict[str, SourceAccuracy], floor: float = WEIGHT_FLOOR) -> dict[str, float]:
    """Edge-over-coin-flip normalization with a per-source floor. Always
    sums to 1.0 (within float precision) and always covers every source in
    SOURCES. Pure, unit-tested."""
    if not (0.0 < floor < 1.0 / len(SOURCES)):
        raise ValueError(f"floor must be in (0, {1.0 / len(SOURCES):.3f}), got {floor}")

    edges = {source: max(accuracies[source].hit_rate - 0.5, 0.0) for source in SOURCES}
    total_edge = sum(edges.values())

    if total_edge <= 0:
        return {source: 1.0 / len(SOURCES) for source in SOURCES}  # all noise -> honest equal weights

    distributable = 1.0 - floor * len(SOURCES)
    return {source: floor + distributable * (edges[source] / total_edge) for source in SOURCES}


def render_scorecard(
    accuracies: dict[str, SourceAccuracy], weights: dict[str, float], as_of: date, lookback_days: int = LOOKBACK_DAYS
) -> str:
    """The vault Scorecard.md -- recall injects this verbatim into every
    portfolio manager prompt, so it's written to be read by both Ryan and
    the model. Pure, unit-tested."""
    lines = [
        "# Signal Source Scorecard",
        "",
        f"As of {as_of.isoformat()} -- directional hit rate of each source's meaningful "
        f"signals (|score| >= {MEANINGFUL_SCORE:g}) against {FORWARD_HORIZON_BARS}-trading-day "
        f"forward returns, over the last {lookback_days} days. Blend weights are recomputed "
        f"weekly from these (floor {WEIGHT_FLOOR:.0%} per source).",
        "",
        "| Source | Scored signals | Hits | Hit rate | Blend weight |",
        "|---|---|---|---|---|",
    ]
    for source in SOURCES:
        acc = accuracies[source]
        hit_rate = f"{acc.hit_rate:.0%}" + (" (default -- too few samples)" if acc.defaulted else "")
        lines.append(f"| {source} | {acc.samples} | {acc.hits} | {hit_rate} | {weights[source]:.0%} |")
    lines.append("")
    return "\n".join(lines)


# ============================================================
# Thin glue -- Supabase + Alpaca + vault IO.
# ============================================================


def retune_weights(supabase_client, settings, vault) -> dict[str, float]:
    """The weekly job: read signal history, score it against forward
    returns, persist new weights + scorecard + audit row. Returns the new
    weights. Raises on Supabase write failure (this IS the job; failing
    silently would leave stale weights looking current) but tolerates
    per-symbol price-fetch failures (one delisted/renamed symbol must not
    kill the whole retune)."""
    from src.db import write_audit_log
    from src.memory.vault import write_scorecard
    from src.signals.momentum import fetch_daily_closes

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = (
        supabase_client.table("signals")
        .select("symbol, signal_type, score, generated_at")
        .gte("generated_at", cutoff)
        .execute()
        .data
        or []
    )

    # One closes fetch per symbol, with a date index for signal alignment.
    symbols = sorted({row["symbol"].upper() for row in rows if row.get("score") is not None})
    closes_by_symbol: dict[str, tuple[list[float], dict[date, int]]] = {}
    for symbol in symbols:
        try:
            closes, dates = fetch_daily_closes_with_dates(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
            closes_by_symbol[symbol] = (closes, {d: i for i, d in enumerate(dates)})
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not kill the retune
            print(f"[{symbol}] closes fetch failed during retune, its signals are skipped: {exc}")

    outcomes: list[SignalOutcome] = []
    for row in rows:
        if row.get("score") is None:
            continue
        symbol = row["symbol"].upper()
        if symbol not in closes_by_symbol:
            continue
        closes, date_index = closes_by_symbol[symbol]
        signal_day = datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00")).date()
        # A signal generated intraday is aligned to that day's close (the
        # first close AT or after the signal -- walk forward across
        # weekends/holidays, bounded so a stale signal can't scan forever).
        index = None
        for offset in range(0, 7):
            index = date_index.get(signal_day + timedelta(days=offset))
            if index is not None:
                break
        if index is None:
            continue
        forward = compute_forward_return(closes, index)
        if forward is None:
            continue
        outcomes.append(
            SignalOutcome(source=row["signal_type"], symbol=symbol, score=float(row["score"]), forward_return=forward)
        )

    accuracies = compute_source_hit_rates(outcomes)
    weights = compute_adaptive_weights(accuracies)
    today = datetime.now(timezone.utc).date()

    config_row = supabase_client.table("config").select("id").limit(1).execute().data[0]
    supabase_client.table("config").update({"signal_blend_weights": weights}).eq("id", config_row["id"]).execute()

    write_scorecard(vault, render_scorecard(accuracies, weights, as_of=today))

    write_audit_log(
        supabase_client,
        event_type="weight_retune",
        decision="weights_updated",
        reasoning=(
            "weekly adaptive retune from "
            + "; ".join(
                f"{s}: {accuracies[s].hits}/{accuracies[s].samples} hits"
                + (" (defaulted)" if accuracies[s].defaulted else "")
                for s in SOURCES
            )
        ),
        metadata={"weights": weights, "outcomes_scored": len(outcomes)},
    )
    return weights


def fetch_daily_closes_with_dates(symbol: str, api_key: str, secret_key: str, lookback_days: int = 90):
    """Like momentum.fetch_daily_closes but also returns each bar's date --
    the tuner needs to align signal timestamps to bar indices, which the
    momentum signal never did. Kept here (not in momentum.py) because this
    is the only caller that needs dates."""
    from datetime import datetime as dt

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(api_key, secret_key)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=dt.now(timezone.utc) - timedelta(days=lookback_days),
    )
    bars = client.get_stock_bars(request).data.get(symbol, [])
    closes = [float(bar.close) for bar in bars]
    dates = [bar.timestamp.date() for bar in bars]
    return closes, dates
