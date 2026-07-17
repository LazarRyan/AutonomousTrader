"""
Per-symbol fundamentals context -- sector, valuation, next earnings date.

Not a fifth scored signal source (deliberately): fundamentals here are
CONTEXT handed to the portfolio manager prompt, not a -100..100 number
blended into the score. Reducing "P/E of 12 in a beaten-down sector with
earnings on Thursday" to one scalar would throw away exactly the nuance a
portfolio manager is for; the deterministic layers that need hard numbers
(risk scorer, safety rails, churn guard) don't use fundamentals at all.

Two concrete decisions this feeds (see the portfolio manager system
prompt): the earnings-blackout guidance (don't initiate a new position
within 2 trading days of an earnings print without explicitly reasoning
about it) and sector awareness (the newsletter's sector-concentration
report, and the model seeing that its five "different" buys are all the
same sector bet).

Data source: yfinance (Yahoo Finance). Free, no key, best-effort -- Yahoo
throttles and changes shape without notice, so EVERY field is optional and
a fully-failed fetch returns a snapshot of Nones rather than raising. A
missing fundamental must never block a trade decision; the prompt simply
says less. Same tolerance discipline as gather_signal_snapshot's
per-source try/except.

Split, same as everywhere:
  1. PURE, unit-tested (tests/test_fundamentals.py):
     render_market_context() and days_until() -- deterministic text/date
     math from typed inputs.
  2. Thin glue: fetch_fundamentals() (yfinance call).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FundamentalsSnapshot:
    symbol: str
    sector: str | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    revenue_growth: float | None = None  # yoy fraction, e.g. 0.12 = +12%
    next_earnings_date: date | None = None


@dataclass(frozen=True)
class MarketRegime:
    """Output of src.risk.regime.assess_market_regime -- carried here too
    because render_market_context is where it becomes prompt text."""

    label: str  # "risk_on" | "neutral" | "risk_off"
    sizing_multiplier: float  # 1.0 normal, <1.0 = size down (risk_off)
    reasoning: str


def days_until(target: date | None, today: date) -> int | None:
    """Calendar days from today to target; None if no target. Negative
    means the date already passed (stale data from the source -- rendered
    as 'recently reported' rather than a confusing negative countdown).
    Pure, unit-tested."""
    if target is None:
        return None
    return (target - today).days


def _render_one_symbol(snapshot: FundamentalsSnapshot, today: date) -> str:
    parts: list[str] = []
    if snapshot.sector:
        parts.append(f"sector: {snapshot.sector}")
    if snapshot.trailing_pe is not None:
        parts.append(f"trailing P/E {snapshot.trailing_pe:.1f}")
    if snapshot.forward_pe is not None:
        parts.append(f"forward P/E {snapshot.forward_pe:.1f}")
    if snapshot.revenue_growth is not None:
        parts.append(f"revenue growth {snapshot.revenue_growth:+.1%} yoy")

    days = days_until(snapshot.next_earnings_date, today)
    if days is not None:
        if days < 0:
            parts.append("earnings recently reported")
        elif days <= 5:
            parts.append(f"EARNINGS IN {days} DAY(S) ({snapshot.next_earnings_date.isoformat()})")
        else:
            parts.append(f"next earnings {snapshot.next_earnings_date.isoformat()} ({days}d)")

    if not parts:
        return f"  {snapshot.symbol}: (no fundamentals available)"
    return f"  {snapshot.symbol}: " + ", ".join(parts)


def render_market_context(
    fundamentals: list[FundamentalsSnapshot],
    regime: MarketRegime | None,
    today: date,
) -> str:
    """Assemble the market-context block for the portfolio manager prompt.
    Deterministic, pure, unit-tested. Empty inputs render explicit
    placeholders (same reasoning as recall's build_memory_context: the
    model should know context was empty, not guess whether it was
    omitted)."""
    if fundamentals:
        fundamentals_lines = "\n".join(_render_one_symbol(s, today) for s in sorted(fundamentals, key=lambda s: s.symbol))
    else:
        fundamentals_lines = "  (no fundamentals available this cycle)"

    if regime is not None:
        regime_line = f"  {regime.label} (sizing multiplier {regime.sizing_multiplier:g}): {regime.reasoning}"
    else:
        regime_line = "  (regime assessment unavailable this cycle)"

    return (
        f"Market regime:\n{regime_line}\n\n"
        f"Fundamentals (context only -- not part of the blended score):\n{fundamentals_lines}"
    )


# ============================================================
# Thin glue -- yfinance fetch, best-effort per field.
# ============================================================


def fetch_fundamentals(symbol: str) -> FundamentalsSnapshot:
    """Best-effort yfinance pull for one symbol. Never raises -- any
    failure (network, throttle, shape change, missing field) degrades to
    None for that field, and a total failure returns an all-None snapshot.
    See module docstring for why this tolerance is deliberate."""
    sector = trailing_pe = forward_pe = revenue_growth = None
    next_earnings: date | None = None
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        sector = info.get("sector") or None
        trailing_pe = _as_float(info.get("trailingPE"))
        forward_pe = _as_float(info.get("forwardPE"))
        revenue_growth = _as_float(info.get("revenueGrowth"))

        try:
            calendar = ticker.calendar or {}
            earnings_dates = calendar.get("Earnings Date") or []
            if earnings_dates:
                first = earnings_dates[0]
                next_earnings = first if isinstance(first, date) else None
        except Exception:  # noqa: BLE001 -- calendar shape churns more than .info; field-level tolerance
            pass
    except Exception as exc:  # noqa: BLE001 -- fundamentals are context, never a blocker
        print(f"[{symbol}] fundamentals fetch failed, continuing without them: {exc}")

    return FundamentalsSnapshot(
        symbol=symbol.upper(),
        sector=sector,
        trailing_pe=trailing_pe,
        forward_pe=forward_pe,
        revenue_growth=revenue_growth,
        next_earnings_date=next_earnings,
    )


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
