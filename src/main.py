"""
Entry point -- the scheduled run loop, now scheduled 3x/day (10:00am/12:45pm/
3:30pm ET via launchd -- see scripts/launchd/) rather than continuously,
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
  11. Best-effort macOS notifications (src/notify.py): one the moment a
      trade lands in approval_queue (so it doesn't just wait silently until
      someone happens to check the dashboard), and one at the end of the
      cycle summarizing what happened (executed/queued/blocked/no trades).
      Never affects any decision above -- see notify.py's own docstring.

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
NotifyApprovalNeededFn = Callable[[CandidateTradeProposal, RiskScoreResult], None]

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
    notify_approval_needed: NotifyApprovalNeededFn | None = None,
    cash_available: float | None = None,
) -> str:
    """Dispatch decision for one proposed trade: score it, persist the
    candidate_trades row (always -- taken or not, per the audit-log
    discipline), and either route to auto-execution or the approval queue.
    Fully unit-tested via injected fakes -- see tests/test_main.py.

    cash_available feeds the no-margin safety rail at execution time (see
    risk/safety_rails.py) -- run_cycle passes its running remaining-cash
    figure so a sequence of buys within one cycle can't collectively
    overspend what any single one of them would have been allowed to.

    notify_approval_needed, if given, is called with (proposal, risk_result)
    the moment a trade is routed to the approval queue -- this is what lets
    a human find out a trade is waiting on them without having to poll the
    dashboard or run scripts/review_approvals.py speculatively. Optional and
    defaults to None (no-op) so this stays a pure dispatch decision by
    default, same DI pattern as every other dependency here; run_cycle()
    wires in a real macOS-notification closure (see src/notify.py).
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
        if notify_approval_needed is not None:
            notify_approval_needed(proposal, risk_result)
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
        cash_available=cash_available,
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
    supabase_client=None,
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

    supabase_client, if given, gets one src.db.write_signal() row per source
    that actually produced a result this cycle (best-effort -- a write
    failure is caught and logged inside write_signal itself, never raised
    here). This is what makes each source's full reasoning/component
    breakdown -- not just the single number that ends up in
    SignalSnapshot -- queryable after the fact; before this, every one of
    these *Result objects was built, its .reasoning read into nothing, and
    then discarded the moment gather_signal_snapshot returned. Optional and
    defaults to None so tests / callers that don't care about persistence
    (or don't have a client handy) aren't forced to supply one.
    """
    import dataclasses

    from src.agents.portfolio_manager import SignalSnapshot
    from src.signals import congressional, insider_edgar, momentum, news_sentiment

    def _persist(signal_type: str, score: float | None, result) -> None:
        if supabase_client is None or result is None:
            return
        from src.db import write_signal

        write_signal(supabase_client, symbol, signal_type, score, dataclasses.asdict(result))

    momentum_score = None
    try:
        closes = momentum.fetch_daily_closes(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
        momentum_result = momentum.compute_momentum_score(symbol, closes)
        momentum_score = momentum_result.momentum_score
        _persist("momentum", momentum_score, momentum_result)
    except Exception as exc:  # noqa: BLE001 -- one bad source must not take out the whole symbol
        print(f"[{symbol}] momentum signal failed, continuing without it: {exc}")

    insider_score = None
    try:
        cik = (cik_map or {}).get(symbol.upper())
        if not cik:
            cik = insider_edgar.fetch_ticker_cik_map(sec_user_agent).get(symbol.upper())
        if cik:
            insider_result = insider_edgar.fetch_insider_signal(symbol, cik, sec_user_agent)
            insider_score = insider_result.score
            _persist("insider", insider_score, insider_result)
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
            congressional_result = congressional.compute_congressional_signal(transactions)
            congressional_score = congressional_result.score
            _persist("congressional", congressional_score, congressional_result)
    except Exception as exc:  # noqa: BLE001
        print(f"[{symbol}] congressional signal failed, continuing without it: {exc}")

    news_score = None
    try:
        if prefetched_headlines is not None and symbol.upper() in prefetched_headlines:
            headlines = prefetched_headlines[symbol.upper()]
        else:
            headlines = news_sentiment.fetch_recent_news(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
        if headlines:
            record_llm_call = None
            if supabase_client is not None:
                from src.llm_metering import make_recorder

                record_llm_call = make_recorder(supabase_client, "news_sentiment")
            news_result = news_sentiment.score_news_sentiment(
                symbol, headlines, settings.anthropic_api_key, record_llm_call=record_llm_call
            )
            news_score = news_result.score
            _persist("news_sentiment", news_score, news_result)
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
    congressional_discovery_cap: int = 15,
) -> None:
    """The actual scheduled entry point. Not unit-tested -- real network
    calls throughout. The pieces that matter for correctness
    (is_trading_halted, process_candidate_trade) are tested in isolation;
    this function is glue.

    UNIVERSE, redesigned for the 3x/day launchd schedule (10:00am/12:45pm/
    3:30pm ET -- see scripts/launchd/): when `universe` isn't passed in
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
      3. Tickers mentioned in House PTR filings filed in the last
         congressional.HOUSE_PTR_LOOKBACK_DAYS days (30 -- deliberately NOT
         "since the last cycle" like news; PTRs lag trades by weeks and only
         a handful are filed per day, so the shared hours-long window found
         ~zero filings per cycle and the signal never accumulated scorecard
         samples). Same ranking/filter, but capped at the smaller
         `congressional_discovery_cap` to bound per-symbol API/LLM cost.
         (Senate discovery isn't wired in yet -- see
         scripts/debug_senate_listing.py for why.)

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
    from src.universe import fetch_tradable_universe, load_sp500_cik_map, load_sp500_universe

    # Full-market universe (2026-07-16): discovery is no longer filtered to
    # the S&P 500 -- any active, tradable, exchange-listed US equity is
    # eligible when it shows up in news or congressional filings. The
    # bundled S&P list remains the fallback when the Alpaca asset-master
    # fetch fails: loudly degraded beats silently tiny.
    try:
        valid_universe_set = fetch_tradable_universe(alpaca_trading_client)
        print(f"discovery universe: {len(valid_universe_set)} tradable exchange-listed symbols")
    except Exception as exc:  # noqa: BLE001
        valid_universe_set = set(load_sp500_universe())
        print(f"tradable-universe fetch failed, falling back to bundled S&P 500 list: {exc}")

    # CIK map for the insider signal: the bundled S&P CSV covers only 500
    # names, so with the full-market universe the complete SEC ticker->CIK
    # file is fetched ONCE per cycle and merged over it (best-effort -- the
    # insider signal simply has no CIK, and therefore no score, for symbols
    # missing from whatever map we end up with; every other signal still
    # works). Without this merge, gather_signal_snapshot's per-symbol
    # fallback would re-download the same SEC file for every non-S&P symbol
    # in the cycle.
    cik_map = load_sp500_cik_map()
    try:
        from src.signals.insider_edgar import fetch_ticker_cik_map

        cik_map = {**fetch_ticker_cik_map(sec_user_agent), **cik_map}
    except Exception as exc:  # noqa: BLE001
        print(f"full SEC CIK map fetch failed, insider signal limited to bundled S&P names this cycle: {exc}")

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
        HoldingSnapshot(
            symbol=p.symbol,
            quantity=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            # Live mark + unrealized P&L (2026-07-17) -- see HoldingSnapshot's
            # docstring for why. getattr-guarded: None degrades the prompt to
            # the old entry-price-only rendering, never fakes a number.
            current_price=(float(p.current_price) if getattr(p, "current_price", None) is not None else None),
            unrealized_pnl_pct=(float(p.unrealized_plpc) if getattr(p, "unrealized_plpc", None) is not None else None),
        )
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
                [article.symbols for article in news_articles], cap=discovery_cap, valid_universe=valid_universe_set
            )
        except Exception as exc:  # noqa: BLE001 -- one discovery source must not take out the whole cycle
            print(f"broad news discovery pull failed, continuing without it: {exc}")

        congressional_discovered: list[str] = []
        try:
            from datetime import timedelta

            # Congressional gets its OWN lookback window, NOT the news-style
            # hours-since-last-cycle `lookback_start` it used to share (the
            # 2026-07-21 fix). PTRs are disclosed up to 30-45 days after the
            # trade and only a handful are filed per day, so the shared
            # hours-long window found ~zero filings on nearly every cycle --
            # the signal scored 3 rows EVER (vs ~1,700 per other source) and
            # the weekly scorecard showed "congressional: 0 scored, default
            # 0.5 hit rate". A 30-day window is also what the signal's
            # net-buying aggregate is meant to summarize. Affordable because
            # per-filing parse results are cached on disk (cache_dir) -- each
            # PDF is fetched/parsed once, not once per cycle.
            congressional_since = today - timedelta(days=congressional.HOUSE_PTR_LOOKBACK_DAYS)
            house_transactions: list = []
            # fetch_recent_house_ptr_transactions only queries one calendar
            # year's disclosure index -- span the year boundary explicitly
            # if the lookback window crosses it (e.g. an early-January run
            # looking back into December).
            for year in sorted({congressional_since.year, today.year}):
                result = congressional.fetch_recent_house_ptr_transactions(
                    year=year,
                    user_agent=sec_user_agent,
                    since=congressional_since,
                    until=today,
                    cache_dir=congressional.DEFAULT_HOUSE_PTR_CACHE_DIR,
                )
                house_transactions.extend(result.transactions)

            congressional_transactions_by_ticker = congressional.aggregate_transactions_by_ticker(house_transactions)
            # Smaller cap than news on purpose: a 30-day filing window
            # surfaces far more tickers than an hours-long news window, and
            # every universe symbol costs real per-symbol API/LLM calls.
            # Top names by mention count over a rolling window are stable
            # cycle-to-cycle, which is exactly what the weight tuner needs
            # to accumulate scored congressional outcomes.
            congressional_discovered = rank_discovered_symbols(
                [[txn.ticker] for txn in house_transactions],
                cap=congressional_discovery_cap,
                valid_universe=valid_universe_set,
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
                f"news lookback since {lookback_start}, congressional lookback "
                f"{congressional.HOUSE_PTR_LOOKBACK_DAYS} days"
            ),
            metadata={"universe": universe},
        )

    # ---- Memory recall (best-effort): the agent's own recent actions,
    # position theses, lessons, and signal scorecard, injected into the
    # portfolio manager prompt so cycles stop being stateless (see
    # src/memory/recall.py for the KHC repeat-buy incident that motivated
    # this). recent_actions is fetched once and reused by the churn guard
    # below. A memory failure degrades to the old stateless prompt -- it
    # must never stop a trading cycle.
    from src.memory.recall import RecentAction, build_memory_context, fetch_recent_actions
    from src.memory.vault import Vault, append_journal_entry, append_position_history, read_lessons, read_position_thesis, read_scorecard

    vault = Vault()
    recent_actions: list[RecentAction] = []
    memory_context: str | None = None
    try:
        recent_actions = fetch_recent_actions(supabase_client)
        theses = {}
        for held in sorted(held_symbols):
            thesis = read_position_thesis(vault, held)
            if thesis:
                theses[held] = thesis
        memory_context = build_memory_context(recent_actions, theses, read_lessons(vault), read_scorecard(vault))

        # ---- Thesis exit-target check (2026-07-17): the deterministic half
        # of "reflection sets targets, cycles check them". The reflection
        # writes concrete exit prices into each thesis; here every held
        # position's live price is compared against them, and crossings are
        # flagged LOUDLY in the prompt (plus an audit row). The decision to
        # actually sell stays with the portfolio manager -- a stop that
        # auto-fires is a different (deterministic-rail) design; this one
        # makes the thesis's own exit condition impossible to not see.
        from src.memory.vault import parse_exit_targets

        price_by_symbol = {h.symbol.upper(): h.current_price for h in holdings if h.current_price is not None}
        target_alerts: list[str] = []
        for held, thesis in theses.items():
            price = price_by_symbol.get(held)
            if price is None:
                continue
            exit_above, exit_below = parse_exit_targets(thesis)
            alert = None
            if exit_above is not None and price >= exit_above:
                alert = (
                    f"{held}: current price ${price:.2f} is AT/ABOVE the thesis profit target "
                    f"${exit_above:,.2f} -- thesis fulfilled, evaluate taking gains"
                )
            elif exit_below is not None and price <= exit_below:
                alert = (
                    f"{held}: current price ${price:.2f} is AT/BELOW the thesis stop "
                    f"${exit_below:,.2f} -- thesis invalidated, evaluate exiting"
                )
            if alert:
                target_alerts.append(f"  {alert}")
                log_audit(event_type="thesis_target", decision="target_crossed", reasoning=alert, symbol=held)
        if target_alerts:
            memory_context += "\n\nTHESIS TARGET ALERTS (a target your own thesis set has been crossed -- address each in your reasoning):\n" + "\n".join(target_alerts)
    except Exception as exc:  # noqa: BLE001 -- memory is an enhancement; stateless is the fallback, not a crash
        print(f"memory recall failed, running this cycle stateless: {exc}")

    # ---- Adaptive blend weights: written weekly by the nightly retune
    # (src/signals/weight_tuner.py) into config.signal_blend_weights; a
    # missing/never-tuned value falls back to the equal-weight defaults
    # inside from_weights.
    try:
        from src.db import get_config

        blend_config = BlendConfig.from_weights(get_config(supabase_client).get("signal_blend_weights"))
    except Exception as exc:  # noqa: BLE001 -- a config-read hiccup shouldn't skip the cycle; defaults are safe
        print(f"could not load adaptive blend weights, using equal-weight defaults: {exc}")
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
            supabase_client=supabase_client,
        )
        try:
            blended_scores[symbol] = compute_blended_signal_score(snapshot, blend_config)
        except ValueError:
            continue  # no signal sources available this cycle for this symbol -- skip, don't guess

    if not blended_scores:
        log_audit(event_type="cycle", decision="cycle_skipped", reasoning="no signal data available for any symbol in universe")
        print("Cycle skipped: no signal data available")
        return

    # ---- Market context (best-effort): fundamentals for the symbols most
    # likely to be traded (held positions + strongest absolute scores,
    # capped -- yfinance is slow and throttles, so no full-universe pull),
    # plus a market regime read. Context for the prompt AND inputs to the
    # deterministic sector/sizing gates below.
    from src.signals.fundamentals import FundamentalsSnapshot

    fundamentals_by_symbol: dict[str, FundamentalsSnapshot] = {}
    regime = None
    market_context: str | None = None
    try:
        from src.signals.fundamentals import fetch_fundamentals, render_market_context
        from src.risk.regime import assess_market_regime, fetch_vix_level
        from src.signals.momentum import fetch_daily_closes

        top_scored = sorted(blended_scores, key=lambda s: abs(blended_scores[s]), reverse=True)[:15]
        for symbol in sorted(held_symbols | set(top_scored)):
            fundamentals_by_symbol[symbol.upper()] = fetch_fundamentals(symbol)

        try:
            spy_closes = fetch_daily_closes(BENCHMARK_SYMBOL, settings.alpaca_api_key, settings.alpaca_secret_key, lookback_days=120)
            regime = assess_market_regime(spy_closes, fetch_vix_level())
        except Exception as exc:  # noqa: BLE001 -- no regime read = no scaling, never a blocker
            print(f"market regime assessment failed, continuing without it: {exc}")

        market_context = render_market_context(list(fundamentals_by_symbol.values()), regime, today)
    except Exception as exc:  # noqa: BLE001
        print(f"market context gathering failed, continuing without it: {exc}")

    # Current $ exposure per sector, for the concentration gate. Built from
    # real Alpaca position market values + this cycle's fundamentals; a held
    # symbol without a sector read simply doesn't contribute (the gate
    # already treats unknown sectors permissively -- see src/risk/sizing.py).
    sector_exposure_value: dict[str, float] = {}
    for p in positions:
        snapshot = fundamentals_by_symbol.get(p.symbol.upper())
        market_value = getattr(p, "market_value", None)
        if snapshot and snapshot.sector and market_value is not None:
            sector_exposure_value[snapshot.sector] = sector_exposure_value.get(snapshot.sector, 0.0) + float(market_value)

    # ---- Context snapshot (2026-07-17): persist the EXACT memory/market
    # context this cycle's prompt carries, before the model sees it.
    # Observability tier 1 -- "what did the agent know when it decided X"
    # becomes a queryable fact instead of a reconstruction. Best-effort:
    # snapshot failure must never cost a trading cycle.
    try:
        supabase_client.table("context_snapshots").insert(
            {
                "memory_context": memory_context,
                "market_context": market_context,
                "memory_chars": len(memory_context or ""),
                "market_chars": len(market_context or ""),
                "metadata": {
                    "universe_size": len(universe),
                    "symbols_with_signals": len(blended_scores),
                    "held_positions": len(holdings),
                    "regime": regime.label if regime else None,
                },
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001
        print(f"context snapshot write failed (non-blocking): {exc}")

    from src.llm_metering import make_recorder

    proposals = propose_candidate_trades(
        blended_scores,
        PortfolioContext(total_portfolio_value=total_portfolio_value, cash_available=cash_available, holdings=holdings),
        settings.anthropic_api_key,
        memory_context=memory_context,
        market_context=market_context,
        record_llm_call=make_recorder(supabase_client, "portfolio_manager"),
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

        # A deliberate no is a decision, and the vault records decisions --
        # found the hard way (2026-07-17): the first cycle to run with
        # memory injected chose restraint on all 59 symbols, and this early
        # return skipped the journal write at the bottom of the function
        # entirely, leaving the day's most interesting decision visible in
        # audit_log but absent from the journal. Best-effort, same as the
        # end-of-cycle journal write below.
        try:
            top_scores = sorted(blended_scores.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
            # [[SYM]] wiki-links (2026-07-17): resolve to Positions/SYM.md in
            # Obsidian for held names, making the journal <-> positions
            # graph navigable (and giving graph-shaped retrieval something
            # real to traverse later).
            scores_text = ", ".join(
                (f"[[{s}]] {v:+.1f}" if s.upper() in held_symbols else f"{s} {v:+.1f}") for s, v in top_scores
            )
            append_journal_entry(
                vault,
                today,
                f"Cycle at {now_eastern_naive.strftime('%H:%M')} ET",
                (
                    f"Universe: {len(universe)} symbol(s), {len(blended_scores)} with usable signals. "
                    f"Regime: {regime.label if regime else 'unavailable'}. "
                    f"**No trades proposed** -- the portfolio manager reviewed the signals plus its memory "
                    f"(recent actions, theses, lessons) and chose restraint. "
                    f"Strongest signals passed over: {scores_text}."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"vault journal write failed (non-blocking): {exc}")

        from src.notify import send_macos_notification

        send_macos_notification(
            title="Autonomous Trader: cycle complete",
            message=f"Scanned {len(blended_scores)} symbol(s) -- no trades proposed this cycle.",
        )
        return

    # ---- Citation verification (2026-07-17): when a proposal's reasoning
    # claims a lesson, deterministically check the claim against the vault
    # (src.memory.recall.verify_lesson_citation). Purely observational --
    # never gates the trade -- but a fabricated citation is exactly the
    # kind of quiet failure the audit trail exists to catch.
    try:
        from src.memory.recall import verify_lesson_citation

        lessons_markdown = read_lessons(vault)
        for proposal in proposals:
            verified = verify_lesson_citation(proposal.reasoning, lessons_markdown)
            if verified is None:
                continue
            log_audit(
                event_type="citation_check",
                decision="citation_verified" if verified else "citation_unverified",
                reasoning=(
                    f"reasoning cites a lesson and a matching vault lesson "
                    f"{'was found' if verified else 'was NOT found -- possible fabricated citation'}: "
                    f"{proposal.reasoning}"
                ),
                symbol=proposal.symbol,
            )
    except Exception as exc:  # noqa: BLE001 -- observational only
        print(f"citation verification failed (non-blocking): {exc}")

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

    def notify_approval_needed(proposal: CandidateTradeProposal, risk_result: RiskScoreResult) -> None:
        from src.notify import send_macos_notification

        send_macos_notification(
            title="Autonomous Trader: trade needs approval",
            message=(
                f"{proposal.symbol} {proposal.side.upper()} x{proposal.quantity:g} -- "
                f"{risk_result.reasoning}. {proposal.reasoning}"
            ),
        )

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

    # One status string per proposal that actually made it to
    # process_candidate_trade (a proposal skipped below for missing
    # price/volatility data never reaches that call, so isn't in here --
    # tallied separately as "skipped" in the end-of-cycle notification).
    cycle_outcomes: list[str] = []

    # Running cash figure for the no-margin rail: starts from the account's
    # real cash at cycle start, decremented by each executed buy so several
    # buys in one cycle can't collectively spend past zero (the account
    # snapshot alone wouldn't catch that -- orders here are placed seconds
    # apart, faster than fills settle into account.cash). Deliberately NOT
    # credited back by sells: sell proceeds aren't reliably reflected until
    # they settle, so within a cycle this stays conservative.
    remaining_cash = cash_available

    # ---- Anti-churn guard inputs: the guard is deterministic (see
    # src/risk/churn_guard.py -- it ENFORCES what the memory prompt only
    # asks for) and runs on the same recent_actions recall already fetched.
    import dataclasses as _dataclasses
    from datetime import datetime as _datetime, timezone as _timezone

    from src.risk.churn_guard import PastExecution, evaluate_churn
    from src.risk.sizing import (
        evaluate_sector_concentration,
        scale_buy_quantity_for_regime,
        scale_buy_quantity_for_volatility,
    )

    past_executions = [
        PastExecution(
            symbol=action.symbol,
            side=action.side,
            status=action.status,
            blended_signal_score=action.blended_signal_score,
            executed_at=_datetime.fromisoformat(action.created_at.replace("Z", "+00:00")),
        )
        for action in recent_actions
    ]
    guard_now = _datetime.now(_timezone.utc)

    for proposal in proposals:
        # ---- Churn gate first: cheapest check, and a suppressed repeat
        # shouldn't cost quote/volatility API calls. A suppressed proposal
        # is a real decision -> audit_log row, same as everything else.
        churn = evaluate_churn(
            proposal.symbol, proposal.side, blended_scores.get(proposal.symbol), past_executions, now=guard_now
        )
        if not churn.allowed:
            log_audit(
                event_type="churn_guard",
                decision="churn_suppressed",
                reasoning=churn.reason,
                symbol=proposal.symbol,
            )
            print(f"[{proposal.symbol}] churn guard suppressed {proposal.side}: {churn.reason}")
            continue

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

        # ---- Hard liquidity floor (full-market universe, 2026-07-16):
        # binary eligibility, distinct from the graded liquidity penalty
        # fed to the scorer -- see LiquidityFloorConfig's docstring. Only
        # gates BUYS: if a held position has decayed below the floor, the
        # one thing we must never block is getting out of it.
        if proposal.side == "buy":
            from src.risk.market_data import passes_liquidity_floor

            floor_ok, floor_reason = passes_liquidity_floor(asset_closes, asset_volumes)
            if not floor_ok:
                log_audit(
                    event_type="liquidity_floor",
                    decision="liquidity_floor_blocked",
                    reasoning=floor_reason,
                    symbol=proposal.symbol,
                )
                print(f"[{proposal.symbol}] liquidity floor blocked buy: {floor_reason}")
                continue

        # ---- Deterministic buy sizing: regime first, then volatility
        # normalization (src/risk/sizing.py). Sells pass through untouched.
        scaled_quantity = scale_buy_quantity_for_regime(proposal.quantity, proposal.side, regime)
        scaled_quantity = scale_buy_quantity_for_volatility(
            scaled_quantity, proposal.side, asset_30d_volatility, benchmark_30d_volatility
        )
        if scaled_quantity != proposal.quantity:
            log_audit(
                event_type="sizing",
                decision="quantity_scaled",
                reasoning=(
                    f"buy quantity scaled {proposal.quantity:g} -> {scaled_quantity:g} "
                    f"(regime: {regime.label if regime else 'unavailable'}, "
                    f"vol ratio {asset_30d_volatility / benchmark_30d_volatility:.2f}x benchmark)"
                ),
                symbol=proposal.symbol,
            )
            proposal = _dataclasses.replace(proposal, quantity=scaled_quantity)

        # ---- Sector concentration gate (buys only, see src/risk/sizing.py).
        proposal_fundamentals = fundamentals_by_symbol.get(proposal.symbol.upper())
        sector_decision = evaluate_sector_concentration(
            side=proposal.side,
            trade_value=proposal.quantity * proposed_price,
            symbol_sector=proposal_fundamentals.sector if proposal_fundamentals else None,
            sector_exposure_value=sector_exposure_value,
            total_portfolio_value=total_portfolio_value,
        )
        if not sector_decision.allowed:
            log_audit(
                event_type="sector_cap",
                decision="sector_cap_blocked",
                reasoning=sector_decision.reason,
                symbol=proposal.symbol,
            )
            print(f"[{proposal.symbol}] sector cap blocked buy: {sector_decision.reason}")
            continue

        outcome = process_candidate_trade(
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
            notify_approval_needed=notify_approval_needed,
            cash_available=remaining_cash,
        )
        cycle_outcomes.append(outcome)
        if outcome == "executed" and proposal.side == "buy":
            remaining_cash -= proposal.quantity * proposed_price
            # Keep the sector ledger current within the cycle so several
            # same-sector buys can't collectively bust the cap that each
            # would individually have cleared (same running-total reasoning
            # as remaining_cash above).
            if proposal_fundamentals and proposal_fundamentals.sector:
                sector_exposure_value[proposal_fundamentals.sector] = (
                    sector_exposure_value.get(proposal_fundamentals.sector, 0.0)
                    + proposal.quantity * proposed_price
                )

        # Vault position history for anything that actually changed the
        # position -- this is what makes Positions/<SYM>.md a complete
        # action log the nightly reflection builds theses on. Best-effort:
        # a vault write failure must never affect trading.
        if outcome == "executed":
            try:
                # [[date]] links the history line back to that day's journal
                # note -- positions <-> journal becomes a navigable graph in
                # Obsidian (both directions: journal entries link [[SYM]]).
                append_position_history(
                    vault,
                    proposal.symbol,
                    f"[[{today.isoformat()}]]: {proposal.side.upper()} x{proposal.quantity:g} @ ~${proposed_price:.2f} "
                    f"(signal {blended_scores.get(proposal.symbol, 0):+.1f}) -- {proposal.reasoning}",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{proposal.symbol}] vault position-history write failed (non-blocking): {exc}")

    # ---- Cycle journal entry (best-effort). One section per scheduled
    # cycle in the day's note; the nightly reflection appends its own
    # section to the same file after the close.
    try:
        from collections import Counter as _Counter

        outcome_counts = dict(_Counter(cycle_outcomes))
        append_journal_entry(
            vault,
            today,
            f"Cycle at {now_eastern_naive.strftime('%H:%M')} ET",
            (
                f"Universe: {len(universe)} symbol(s), {len(blended_scores)} with usable signals. "
                f"Regime: {regime.label if regime else 'unavailable'}. "
                f"Proposals: {len(proposals)}; outcomes: {outcome_counts or 'none reached execution stage'}.\n\n"
                + "\n".join(
                    f"- [[{p.symbol}]] {p.side.upper()} x{p.quantity:g} (signal {blended_scores.get(p.symbol, 0):+.1f}) -- {p.reasoning}"
                    for p in proposals
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"vault journal write failed (non-blocking): {exc}")

    _notify_cycle_summary(len(proposals), cycle_outcomes)


def _notify_cycle_summary(num_proposals: int, outcomes: list[str]) -> None:
    """Best-effort 'the scheduled run finished, here's what happened' macOS
    notification -- fired once at the end of a cycle that reached the
    propose/execute stage. Deliberately NOT called from any of the earlier
    'nothing to do' skip branches in run_cycle() (kill switch/loss halt,
    non-trading day, market already closed, no signal data, benchmark
    volatility unavailable) -- those would otherwise fire 3x/day on every
    routine weekend and holiday with nothing new to say. The queued-for-
    approval notification (see notify_approval_needed above) already covers
    the one skip-adjacent outcome that's genuinely time-sensitive.
    """
    from collections import Counter

    from src.notify import send_macos_notification

    counts = Counter(outcomes)
    skipped = num_proposals - len(outcomes)  # missing price/volatility data, see loop above

    parts = []
    if counts["executed"]:
        parts.append(f"{counts['executed']} executed")
    if counts["queued_for_approval"]:
        parts.append(f"{counts['queued_for_approval']} queued for approval")
    if counts["blocked"]:
        parts.append(f"{counts['blocked']} blocked")
    if counts["execution_failed"]:
        parts.append(f"{counts['execution_failed']} failed")
    if counts["refused_live_mode"]:
        parts.append(f"{counts['refused_live_mode']} refused (live mode)")
    if skipped:
        parts.append(f"{skipped} skipped (no price/volatility data)")

    summary = ", ".join(parts) if parts else "no outcomes recorded"
    send_macos_notification(
        title="Autonomous Trader: cycle complete",
        message=f"{num_proposals} trade(s) proposed -- {summary}.",
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
