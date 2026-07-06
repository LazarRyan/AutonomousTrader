"""
Entry point -- the scheduled run loop, now scheduled 3x/day (9:40am/2:30pm/
3:50pm ET via launchd -- see scripts/launchd/) rather than continuously,
that ties everything else together:

  1. Load config + safety_state from Supabase.
  2. If kill switch engaged or daily/weekly halted -> log and exit early.
  3. If today isn't a trading day, or the market's already closed for the
     day (early close), log and exit early.
  4. Build this cycle's trading universe (see UNIVERSE NOTE below).
  5. Generate signals (momentum, insider, congressional, news sentiment)
     for every symbol in that universe.
  6. Blend signals per symbol (agents.portfolio_manager.compute_blended_signal_score).
  7. Portfolio Manager Agent proposes candidate trades.
  8. Risk scorer scores each candidate (risk.scorer.score_trade).
  9. Below threshold -> Execution Agent (paper only). At/above -> approval_queue.
  10. Everything, taken or not, gets an audit_log row.

Same split as the rest of the project:

  1. PURE, unit-tested logic:
       - is_trading_halted(): the kill-switch/loss-halt short-circuit check
         (step 2 above). Trivial but safety-critical, so it's a named,
         tested function rather than an inline if-statement easy to get
         wrong under future edits.
       - process_candidate_trade(): the dispatch decision for ONE proposed
         trade -- score it, persist it, and either hand it to the Execution
         Agent (auto-approve path) or the approval queue (needs-approval
         path). Fully unit-tested via injected fake dependencies, same
         pattern as agents/execution.py and scripts/review_approvals.py.
         This is the piece that decides "does this trade auto-fire or wait
         for a human," so it gets full coverage.
       - The universe-discovery MATH (src.discovery.rank_discovered_symbols,
         compute_lookback_start) is pure and fully tested there; only the
         network calls that feed it are untested glue, same split as ever.

  2. Thin, not-unit-tested glue: gather_signal_snapshot() (calls all four
     signal-source fetchers for one symbol, tolerating individual source
     failures), run_cycle() (the actual scheduled entry point), and main().

UNIVERSE NOTE (redesigned -- see run_cycle()'s own docstring for the full
reasoning): run_cycle() used to default to scanning the full static S&P 500
list (src.universe.load_sp500_universe(), backed by
data/sp500_constituents.csv) every single cycle. Now that it's scheduled
3x/day instead of continuously, the default is instead built fresh each run
from real current Alpaca positions plus whatever tickers actually showed up
in recent news or congressional PTR filings -- a much smaller, much more
relevant set. DEFAULT_EXAMPLE_UNIVERSE below is unrelated to either of
these -- it's only for passing an explicit small override into run_cycle()
during manual local dry runs (scripts/dry_run.py), which bypasses dynamic
discovery entirely and uses exactly the list you give it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.agents.execution import ExecutionRequest, ExecutionResult, execute_trade
from src.agents.portfolio_manager import CandidateTradeProposal
from src.risk.safety_rails import SafetyConfig, SafetyState
from src.risk.scorer import RiskScoreResult, RiskScorerConfig, TradeRiskInputs, score_trade

LogAuditFn = Callable[..., None]
ExecuteTradeFn = Callable[[ExecutionRequest], ExecutionResult]
InsertCandidateTradeFn = Callable[[CandidateTradeProposal, float, RiskScoreResult], str]  # -> candidate_trade_id
InsertApprovalQueueItemFn = Callable[[str, RiskScoreResult], None]

# NOT the real S&P 500 -- a small, clearly-labeled example universe for
# local dry runs only. See module docstring.
DEFAULT_EXAMPLE_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]

# Reference symbol for relative-volatility scoring in risk/scorer.py.
BENCHMARK_SYMBOL = "SPY"


def is_trading_halted(safety_state: SafetyState) -> tuple[bool, str]:
    """The step-2 short-circuit: kill switch or either loss halt stops the
    whole cycle before any signal generation or trade proposal happens, not
    just before order placement. Pure, tested function.
    """
    reasons = []
    if safety_state.kill_switch_engaged:
        reasons.append("kill switch engaged")
    if safety_state.daily_halted:
        reasons.append("daily loss limit halt active")
    if safety_state.weekly_halted:
        reasons.append("weekly loss limit halt active")

    if reasons:
        return True, "; ".join(reasons)
    return False, "trading is active"


def process_candidate_trade(
    proposal: CandidateTradeProposal,
    proposed_price: float,
    total_portfolio_value: float,
    asset_30d_volatility: float,
    benchmark_30d_volatility: float,
    liquidity_penalty: float,
    insert_candidate_trade: InsertCandidateTradeFn,
    insert_approval_queue_item: InsertApprovalQueueItemFn,
    execute_trade_fn: ExecuteTradeFn,
    log_audit: LogAuditFn,
    risk_config: RiskScorerConfig | None = None,
) -> str:
    """Dispatch decision for one proposed trade: score it, persist the
    candidate_trades row (always -- taken or not, per the audit-log
    discipline), and either route to auto-execution or the approval queue.
    Fully unit-tested via injected fakes -- see tests/test_main.py.
    """
    trade_value = proposal.quantity * proposed_price

    risk_result = score_trade(
        TradeRiskInputs(
            symbol=proposal.symbol,
            trade_value=trade_value,
            total_portfolio_value=total_portfolio_value,
            asset_30d_volatility=asset_30d_volatility,
            benchmark_30d_volatility=benchmark_30d_volatility,
            liquidity_penalty=liquidity_penalty,
        ),
        config=risk_config,
    )

    # Persisted unconditionally -- this row exists whether the trade ends
    # up auto-executed, queued, or (later) rejected/blocked.
    candidate_trade_id = insert_candidate_trade(proposal, proposed_price, risk_result)

    if risk_result.needs_approval:
        insert_approval_queue_item(candidate_trade_id, risk_result)
        log_audit(
            event_type="risk_scoring",
            decision="queued_for_approval",
            reasoning=risk_result.reasoning,
            symbol=proposal.symbol,
            candidate_trade_id=candidate_trade_id,
        )
        return "queued_for_approval"

    log_audit(
        event_type="risk_scoring",
        decision="auto_approved",
        reasoning=risk_result.reasoning,
        symbol=proposal.symbol,
        candidate_trade_id=candidate_trade_id,
    )

    request = ExecutionRequest(
        candidate_trade_id=candidate_trade_id,
        symbol=proposal.symbol,
        side=proposal.side,
        quantity=proposal.quantity,
        trade_value=trade_value,
        total_portfolio_value=total_portfolio_value,
    )
    result = execute_trade_fn(request)
    return result.status


# ============================================================
# Thin glue -- not unit-tested here (real network calls across four signal
# sources, an LLM call, and Supabase/Alpaca writes).
# ============================================================


@dataclass(frozen=True)
class MarketContext:
    """Per-symbol inputs the risk scorer needs that aren't part of any
    signal source: current price, volatility, and a liquidity penalty.
    Gathering these accurately (e.g. real 30-day volatility, a real
    liquidity metric from volume/spread data) is its own piece of work --
    gather_signal_snapshot below computes simple placeholders where a full
    implementation doesn't exist yet, clearly marked as such.
    """

    proposed_price: float
    asset_30d_volatility: float
    benchmark_30d_volatility: float
    liquidity_penalty: float


def gather_signal_snapshot(
    symbol: str,
    settings,
    sec_user_agent: str,
    cik_map: dict[str, str] | None = None,
    prefetched_headlines: dict[str, list[str]] | None = None,
    congressional_transactions_by_ticker: dict[str, list] | None = None,
):
    """Call all four signal sources for one symbol, tolerating individual
    source failures (a down/rate-limited source shouldn't take out the
    whole symbol -- compute_blended_signal_score already handles partial
    signal availability). Returns a SignalSnapshot with whichever scores
    were obtainable this cycle.

    cik_map should be the bundled src.universe.load_sp500_cik_map() result,
    passed in once per cycle by the caller -- NOT refetched from SEC on
    every symbol, which would mean re-downloading and re-parsing the same
    multi-thousand-entry ticker->CIK file 500 times per cycle for no
    reason. Falls back to a live SEC fetch only if no map is supplied.

    prefetched_headlines and congressional_transactions_by_ticker are the
    per-cycle discovery results built once by run_cycle() (a single broad
    news pull and a single recent-filings pull, respectively -- see
    run_cycle()'s docstring). When this symbol is present in either dict,
    its data is used directly instead of a second, redundant network call;
    a symbol NOT covered by discovery (e.g. a held position with zero
    recent news or filings) falls back to the old per-symbol
    fetch_recent_news call, and simply gets no congressional score (None,
    same as before congressional discovery existed) if it has no
    aggregated transactions.
    """
    from src.agents.portfolio_manager import SignalSnapshot
    from src.signals import congressional, insider_edgar, momentum, news_sentiment

    momentum_score = None
    try:
        closes = momentum.fetch_daily_closes(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
        momentum_score = momentum.compute_momentum_score(symbol, closes).momentum_score
    except Exception as exc:  # noqa: BLE001 -- one bad source must not take out the whole symbol
        print(f"[{symbol}] momentum signal failed, continuing without it: {exc}")

    insider_score = None
    try:
        cik = (cik_map or {}).get(symbol.upper())
        if not cik:
            cik = insider_edgar.fetch_ticker_cik_map(sec_user_agent).get(symbol.upper())
        if cik:
            insider_score = insider_edgar.fetch_insider_signal(symbol, cik, sec_user_agent).score
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] insider signal failed, continuing without it: {exc}")

    # Congressional signal: uses this cycle's already-fetched, already-
    # aggregated House PTR transactions (built once by run_cycle() via
    # congressional.fetch_recent_house_ptr_transactions +
    # aggregate_transactions_by_ticker) -- no per-symbol network call here
    # at all. A symbol with no recent filed transactions correctly gets no
    # score (None), same as before this signal was wired in. Senate
    # discovery is not yet part of this dict -- see
    # scripts/debug_senate_listing.py for why.
    congressional_score = None
    try:
        transactions = (congressional_transactions_by_ticker or {}).get(symbol.upper())
        if transactions:
            congressional_score = congressional.compute_congressional_signal(transactions).score
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] congressional signal failed, continuing without it: {exc}")

    news_score = None
    try:
        if prefetched_headlines is not None and symbol.upper() in prefetched_headlines:
            headlines = prefetched_headlines[symbol.upper()]
        else:
            headlines = news_sentiment.fetch_recent_news(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
        if headlines:
            news_score = news_sentiment.score_news_sentiment(symbol, headlines, settings.anthropic_api_key).score
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] news sentiment signal failed, continuing without it: {exc}")

    return SignalSnapshot(
        symbol=symbol,
        momentum_score=momentum_score,
        insider_score=insider_score,
        congressional_score=congressional_score,
        news_sentiment_score=news_score,
    )


def _previous_trading_session_close(alpaca_trading_client, before):
    """The real close time of the most recent trading session strictly
    before `before` (a date) -- used as the discovery lookback anchor for
    the first scheduled cycle of a trading day, so a Monday morning run
    correctly spans the whole weekend and a post-holiday run spans the
    holiday, without hardcoding a fixed hour count anywhere. Thin glue, not
    unit-tested (real Alpaca calendar call) -- the pure logic that USES this
    value lives in src.discovery.compute_lookback_start.
    """
    from datetime import timedelta

    from alpaca.trading.requests import GetCalendarRequest

    lookback_start = before - timedelta(days=14)  # generous enough to span any real holiday cluster
    calendar = alpaca_trading_client.get_calendar(GetCalendarRequest(start=lookback_start, end=before - timedelta(days=1)))
    if not calendar:
        raise RuntimeError(f"no trading sessions found in the 14 days before {before} -- calendar data problem")
    return calendar[-1].close


def run_cycle(
    supabase_client,
    alpaca_trading_client,
    settings,
    sec_user_agent: str,
    universe: list[str] | None = None,
    discovery_cap: int = 50,
) -> None:
    """The actual scheduled entry point. Not unit-tested -- real network
    calls throughout. The pieces that matter for correctness
    (is_trading_halted, process_candidate_trade) are tested in isolation;
    this function is glue.

    UNIVERSE, redesigned for the 3x/day launchd schedule (9:40am/2:30pm/
    3:50pm ET -- see scripts/launchd/): when `universe` isn't passed in
    explicitly, it's no longer the full static S&P 500 list scanned blindly
    every cycle. It's built fresh each run from three sources, unioned
    together:
      1. Real current Alpaca positions (via get_all_positions() -- NOT the
         Supabase `holdings` table, which record_executed_trade() doesn't
         currently keep in sync; reading directly from Alpaca means this is
         always accurate regardless of that gap).
      2. Tickers mentioned in a single broad (symbol-less) Alpaca news pull
         since the last scheduled cycle, ranked by mention count and capped
         at `discovery_cap`, filtered to the known S&P 500 list (screens
         out OTC/junk tickers the rest of the pipeline -- insider EDGAR's
         CIK map in particular -- isn't built to handle).
      3. Tickers mentioned in House PTR filings filed since the last cycle,
         same ranking/cap/filter. (Senate discovery isn't wired in yet --
         see scripts/debug_senate_listing.py for why.)

    An explicit `universe` argument (e.g. scripts/dry_run.py's small manual
    lists) bypasses all of this and is used as-is, same as before -- the
    dynamic default only applies to the real scheduled entry point.

    This also adds a market-day check: a scheduled (launchd) run firing on
    a weekend or market holiday, or after an early close, now skips cleanly
    with an audit_log row instead of wasting real API calls against a
    closed market.
    """
    from alpaca.trading.requests import GetCalendarRequest

    from src.agents.execution import make_live_dependencies
    from src.agents.portfolio_manager import (
        BlendConfig,
        HoldingSnapshot,
        PortfolioContext,
        compute_blended_signal_score,
        propose_candidate_trades,
    )
    from src.db import write_audit_log
    from src.discovery import compute_lookback_start, rank_discovered_symbols
    from src.signals import congressional, news_sentiment
    from src.universe import load_sp500_cik_map, load_sp500_universe

    cik_map = load_sp500_cik_map()
    sp500_universe_set = set(load_sp500_universe())

    def log_audit(event_type, decision, reasoning, symbol=None, candidate_trade_id=None, metadata=None):
        write_audit_log(
            supabase_client,
            event_type=event_type,
            decision=decision,
            reasoning=reasoning,
            symbol=symbol,
            candidate_trade_id=candidate_trade_id,
            metadata=metadata,
        )

    safety_row = supabase_client.table("safety_state").select("*").limit(1).execute().data[0]
    safety_state = SafetyState(
        kill_switch_engaged=safety_row["kill_switch_engaged"],
        daily_pnl_pct=safety_row["daily_pnl_pct"],
        weekly_pnl_pct=safety_row["weekly_pnl_pct"],
        daily_halted=safety_row["daily_halted"],
        weekly_halted=safety_row["weekly_halted"],
    )

    halted, reason = is_trading_halted(safety_state)
    if halted:
        log_audit(event_type="cycle", decision="cycle_skipped", reasoning=reason)
        print(f"Cycle skipped: {reason}")
        return

    # Market-day / calendar check. Calendar.open/close come back as NAIVE
    # datetimes from alpaca-py (confirmed against its actual source: its
    # Calendar.__init__ builds them with datetime.strptime and no %z), while
    # Clock.timestamp is documented as already being in Eastern time --
    # stripping its tzinfo (not converting) gives the matching Eastern
    # wall-clock value, so the two are safely comparable.
    now_clock = alpaca_trading_client.get_clock()
    now_eastern_naive = now_clock.timestamp.replace(tzinfo=None)
    today = now_clock.timestamp.date()

    todays_calendar = alpaca_trading_client.get_calendar(GetCalendarRequest(start=today, end=today))
    if not todays_calendar:
        log_audit(event_type="cycle", decision="cycle_skipped", reasoning=f"{today} is not a trading day (weekend/holiday)")
        print(f"Cycle skipped: {today} is not a trading day")
        return

    todays_close = todays_calendar[0].close
    if now_eastern_naive > todays_close:
        log_audit(
            event_type="cycle",
            decision="cycle_skipped",
            reasoning=(
                f"market already closed for the day (closed {todays_close}, now {now_eastern_naive}) -- "
                f"likely an early-close day, skipping this scheduled slot rather than running against a closed market"
            ),
        )
        print("Cycle skipped: market already closed today")
        return

    account = alpaca_trading_client.get_account()
    total_portfolio_value = float(account.equity)
    cash_available = float(account.cash)

    # Real Alpaca positions -- see run_cycle()'s docstring for why this
    # replaces the (unsynced) Supabase `holdings` table as the source of
    # truth for both the portfolio context sent to the portfolio manager
    # AND the universe's held-position component below.
    positions = alpaca_trading_client.get_all_positions()
    holdings = [
        HoldingSnapshot(symbol=p.symbol, quantity=float(p.qty), avg_entry_price=float(p.avg_entry_price))
        for p in positions
    ]
    held_symbols = {h.symbol.upper() for h in holdings}

    news_headlines_by_symbol: dict[str, list[str]] = {}
    congressional_transactions_by_ticker: dict[str, list] = {}

    if universe is None:
        try:
            previous_close = _previous_trading_session_close(alpaca_trading_client, today)
        except Exception as exc:  # noqa: BLE001 -- discovery is best-effort; held positions still get scanned
            print(f"could not determine previous trading session close, using a 24h lookback instead: {exc}")
            from datetime import timedelta

            previous_close = now_eastern_naive - timedelta(hours=24)

        lookback_start = compute_lookback_start(now_eastern_naive, previous_close)

        news_discovered: list[str] = []
        try:
            news_articles = news_sentiment.fetch_market_news_window(
                settings.alpaca_api_key, settings.alpaca_secret_key, start=lookback_start, end=now_clock.timestamp
            )
            news_headlines_by_symbol = news_sentiment.bucket_headlines_by_symbol(news_articles)
            news_discovered = rank_discovered_symbols(
                [article.symbols for article in news_articles], cap=discovery_cap, valid_universe=sp500_universe_set
            )
        except Exception as exc:  # noqa: BLE001 -- one discovery source must not take out the whole cycle
            print(f"broad news discovery pull failed, continuing without it: {exc}")

        congressional_discovered: list[str] = []
        try:
            house_transactions: list = []
            # fetch_recent_house_ptr_transactions only queries one calendar
            # year's disclosure index -- span the year boundary explicitly
            # if the lookback window crosses it (e.g. an early-January run
            # looking back into December).
            for year in {lookback_start.date().year, today.year}:
                result = congressional.fetch_recent_house_ptr_transactions(
                    year=year, user_agent=sec_user_agent, since=lookback_start.date(), until=today
                )
                house_transactions.extend(result.transactions)

            congressional_transactions_by_ticker = congressional.aggregate_transactions_by_ticker(house_transactions)
            congressional_discovered = rank_discovered_symbols(
                [[txn.ticker] for txn in house_transactions], cap=discovery_cap, valid_universe=sp500_universe_set
            )
        except Exception as exc:  # noqa: BLE001
            print(f"congressional discovery pull failed, continuing without it: {exc}")

        universe = sorted(held_symbols | set(news_discovered) | set(congressional_discovered))
        log_audit(
            event_type="cycle",
            decision="universe_discovered",
            reasoning=(
                f"dynamic universe for this cycle: {len(held_symbols)} held position(s), "
                f"{len(news_discovered)} news-discovered, {len(congressional_discovered)} "
                f"congressional-discovered ({len(universe)} unique symbol(s) total), "
                f"lookback since {lookback_start}"
            ),
            metadata={"universe": universe},
        )

    blend_config = BlendConfig()
    blended_scores: dict[str, float] = {}
    for symbol in universe:
        snapshot = gather_signal_snapshot(
            symbol,
            settings,
            sec_user_agent,
            cik_map=cik_map,
            prefetched_headlines=news_headlines_by_symbol,
            congressional_transactions_by_ticker=congressional_transactions_by_ticker,
        )
        try:
            blended_scores[symbol] = compute_blended_signal_score(snapshot, blend_config)
        except ValueError:
            continue  # no signal sources available this cycle for this symbol -- skip, don't guess

    if not blended_scores:
        log_audit(event_type="cycle", decision="cycle_skipped", reasoning="no signal data available for any symbol in universe")
        print("Cycle skipped: no signal data available")
        return

    proposals = propose_candidate_trades(
        blended_scores,
        PortfolioContext(total_portfolio_value=total_portfolio_value, cash_available=cash_available, holdings=holdings),
        settings.anthropic_api_key,
    )

    # "The portfolio manager looked at real signal data and decided nothing
    # was worth proposing" is a real, deliberate decision -- it needs its
    # own audit_log row same as every other decision, taken or not. Without
    # this, a cycle that ran a real LLM call and consciously chose to do
    # nothing is indistinguishable in the audit trail from a cycle that
    # never got this far at all (confirmed as a real gap: the first fully
    # clean dry run -- all four signal sources working, thinking disabled,
    # no truncation -- produced zero proposals and, before this fix, wrote
    # nothing to audit_log at all for that outcome).
    if not proposals:
        log_audit(
            event_type="cycle",
            decision="no_trades_proposed",
            reasoning=(
                f"portfolio manager proposed no trades this cycle for "
                f"{len(blended_scores)} symbol(s) with usable signal data -- "
                f"a valid, deliberate outcome, not a failure"
            ),
            metadata={"blended_scores": blended_scores},
        )
        print("Cycle complete: portfolio manager proposed no trades this cycle")
        return

    place_order, _log_audit_unused, record_executed_trade, update_candidate_trade_status = make_live_dependencies(
        supabase_client, alpaca_trading_client
    )

    def execute_trade_fn(request: ExecutionRequest) -> ExecutionResult:
        return execute_trade(
            request,
            trading_mode=settings.trading_mode,
            safety_state=safety_state,
            place_order=place_order,
            log_audit=log_audit,
            record_executed_trade=record_executed_trade,
            update_candidate_trade_status=update_candidate_trade_status,
            safety_config=SafetyConfig(),
        )

    def insert_candidate_trade(proposal: CandidateTradeProposal, proposed_price: float, risk_result: RiskScoreResult) -> str:
        result = (
            supabase_client.table("candidate_trades")
            .insert(
                {
                    "symbol": proposal.symbol,
                    "side": proposal.side,
                    "quantity": proposal.quantity,
                    "proposed_price": proposed_price,
                    "blended_signal_score": blended_scores.get(proposal.symbol),
                    "risk_score": risk_result.composite_score,
                    "risk_breakdown": {
                        "size_component": risk_result.size_component,
                        "volatility_component": risk_result.volatility_component,
                        "liquidity_component": risk_result.liquidity_component,
                    },
                    "status": "queued_for_approval" if risk_result.needs_approval else "auto_approved",
                    "portfolio_manager_reasoning": proposal.reasoning,
                }
            )
            .execute()
        )
        return result.data[0]["id"]

    def insert_approval_queue_item(candidate_trade_id: str, risk_result: RiskScoreResult) -> None:
        supabase_client.table("approval_queue").insert(
            {
                "candidate_trade_id": candidate_trade_id,
                "risk_score": risk_result.composite_score,
                "reasoning": risk_result.reasoning,
            }
        ).execute()

    # Benchmark volatility (SPY by default) is computed once per cycle and
    # reused for every candidate -- risk/scorer.py only cares about the
    # asset/benchmark RATIO, and recomputing the same benchmark 500 times
    # would be pure waste.
    from src.risk.market_data import compute_liquidity_penalty, compute_volatility, fetch_bars_with_volume

    try:
        benchmark_closes, _ = fetch_bars_with_volume(
            BENCHMARK_SYMBOL, settings.alpaca_api_key, settings.alpaca_secret_key
        )
        benchmark_30d_volatility = compute_volatility(benchmark_closes)
    except Exception as exc:  # noqa: BLE001
        log_audit(
            event_type="cycle",
            decision="cycle_skipped",
            reasoning=f"could not compute benchmark ({BENCHMARK_SYMBOL}) volatility, refusing to guess: {exc}",
        )
        print(f"Cycle skipped: benchmark volatility unavailable ({exc})")
        return

    # Real bug found on a live dry run: TradingClient (the ORDER-placement
    # client, used everywhere else in this function) has no
    # get_latest_quote method at all -- quotes come from Alpaca's separate
    # market-data API via StockHistoricalDataClient. Built once here and
    # reused across the proposal loop, same reasoning as the benchmark
    # volatility fetch above (no reason to reconstruct a client 500 times).
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    market_data_client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

    for proposal in proposals:
        try:
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=proposal.symbol, feed=DataFeed.IEX)
            quote = market_data_client.get_stock_latest_quote(quote_request)[proposal.symbol]
            proposed_price = float(quote.ask_price or quote.bid_price)
        except Exception as exc:  # noqa: BLE001
            log_audit(
                event_type="risk_scoring",
                decision="skipped",
                reasoning=f"could not get a current price for {proposal.symbol}, refusing to guess: {exc}",
                symbol=proposal.symbol,
            )
            continue

        try:
            asset_closes, asset_volumes = fetch_bars_with_volume(
                proposal.symbol, settings.alpaca_api_key, settings.alpaca_secret_key
            )
            asset_30d_volatility = compute_volatility(asset_closes)
            liquidity_penalty = compute_liquidity_penalty(asset_closes, asset_volumes)
        except Exception as exc:  # noqa: BLE001
            log_audit(
                event_type="risk_scoring",
                decision="skipped",
                reasoning=(
                    f"could not compute volatility/liquidity for {proposal.symbol}, refusing to "
                    f"guess a risk score without them: {exc}"
                ),
                symbol=proposal.symbol,
            )
            continue

        process_candidate_trade(
            proposal,
            proposed_price=proposed_price,
            total_portfolio_value=total_portfolio_value,
            asset_30d_volatility=asset_30d_volatility,
            benchmark_30d_volatility=benchmark_30d_volatility,
            liquidity_penalty=liquidity_penalty,
            insert_candidate_trade=insert_candidate_trade,
            insert_approval_queue_item=insert_approval_queue_item,
            execute_trade_fn=execute_trade_fn,
            log_audit=log_audit,
        )


def _parse_main_args() -> "argparse.Namespace":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help=(
            "Show account status/mode and ask for an explicit y/n confirmation before "
            "running the cycle. OFF by default so launchd's unattended scheduled runs "
            "(no stdin/TTY attached -- see scripts/launchd/run_cycle.sh) never hang "
            "waiting on input. Pass this flag only for manual/interactive runs, e.g. "
            "the first live test of a new pipeline change: `python -m src.main --confirm`."
        ),
    )
    return parser.parse_args()


def main() -> None:
    from alpaca.trading.client import TradingClient

    from src.config import load_settings
    from src.db import get_client

    args = _parse_main_args()

    settings = load_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY must be set to run the news sentiment signal and portfolio manager agent")

    supabase_client = get_client(settings)
    alpaca_trading_client = TradingClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, paper=not settings.is_live_mode
    )

    import os

    sec_user_agent = os.getenv("SEC_EDGAR_USER_AGENT")
    if not sec_user_agent:
        raise RuntimeError("SEC_EDGAR_USER_AGENT must be set -- see .env.example")

    if args.confirm:
        account = alpaca_trading_client.get_account()
        mode = "LIVE (real money)" if settings.is_live_mode else "paper (fake money)"
        print("=" * 70)
        print("AUTONOMOUS-TRADER -- MANUAL RUN (--confirm)")
        print("=" * 70)
        print(f"Trading mode  : {mode}")
        print(f"Account status: {account.status}")
        print(f"Equity        : ${float(account.equity):,.2f}")
        print(f"Cash          : ${float(account.cash):,.2f}")
        print()
        print("This will run the full scheduled cycle right now: dynamic universe")
        print("discovery (holdings + news + congressional), real signal/API calls,")
        print("and MAY place a real order if the portfolio manager proposes a trade.")
        print()
        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted -- nothing was run.")
            return
        print("\nRunning cycle...\n")

    run_cycle(supabase_client, alpaca_trading_client, settings, sec_user_agent)


if __name__ == "__main__":
    main()
