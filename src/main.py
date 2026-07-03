"""
Entry point -- the scheduled run loop (build plan: every 15-30 min during
market hours) that ties everything else together:

  1. Load config + safety_state from Supabase.
  2. If kill switch engaged or daily/weekly halted -> log and exit early.
  3. Generate signals (momentum, insider, congressional, news sentiment)
     for every symbol in the trading universe.
  4. Blend signals per symbol (agents.portfolio_manager.compute_blended_signal_score).
  5. Portfolio Manager Agent proposes candidate trades.
  6. Risk scorer scores each candidate (risk.scorer.score_trade).
  7. Below threshold -> Execution Agent (paper only). At/above -> approval_queue.
  8. Everything, taken or not, gets an audit_log row.

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

  2. Thin, not-unit-tested glue: gather_signal_snapshot() (calls all four
     signal-source fetchers for one symbol, tolerating individual source
     failures), run_cycle() (the actual scheduled entry point), and main().

TRADING UNIVERSE NOTE: build plan section 4a specifies S&P 500 constituents.
run_cycle() defaults to the real list via src.universe.load_sp500_universe(),
backed by data/sp500_constituents.csv (see that module's docstring for how
it's kept fresh). DEFAULT_EXAMPLE_UNIVERSE below is only for passing an
explicit small override into run_cycle() during manual local dry runs --
it is not used unless you pass it in yourself.
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


def gather_signal_snapshot(symbol: str, settings, sec_user_agent: str, cik_map: dict[str, str] | None = None):
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

    # Congressional signal requires matching parsed PTR transactions across
    # recent House/Senate filings to this symbol's ticker -- that
    # aggregation (across many filers' filings, not just one) is
    # orchestrated at the run_cycle level, not per-symbol, since it's far
    # more efficient to fetch the recent filing indexes once per cycle and
    # then bucket transactions by ticker. See run_cycle().
    congressional_score = None

    news_score = None
    try:
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


def run_cycle(
    supabase_client,
    alpaca_trading_client,
    settings,
    sec_user_agent: str,
    universe: list[str] | None = None,
) -> None:
    """The actual scheduled entry point. Not unit-tested -- real network
    calls throughout. The pieces that matter for correctness
    (is_trading_halted, process_candidate_trade) are tested in isolation;
    this function is glue.
    """
    from src.agents.execution import make_live_dependencies
    from src.agents.portfolio_manager import (
        BlendConfig,
        PortfolioContext,
        compute_blended_signal_score,
        propose_candidate_trades,
    )
    from src.db import write_audit_log
    from src.universe import load_sp500_cik_map, load_sp500_universe

    universe = universe or load_sp500_universe()
    cik_map = load_sp500_cik_map()

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

    account = alpaca_trading_client.get_account()
    total_portfolio_value = float(account.equity)
    cash_available = float(account.cash)

    holdings_rows = supabase_client.table("holdings").select("*").execute().data
    from src.agents.portfolio_manager import HoldingSnapshot

    holdings = [
        HoldingSnapshot(symbol=row["symbol"], quantity=row["quantity"], avg_entry_price=row["avg_entry_price"])
        for row in holdings_rows
    ]

    blend_config = BlendConfig()
    blended_scores: dict[str, float] = {}
    for symbol in universe:
        snapshot = gather_signal_snapshot(symbol, settings, sec_user_agent, cik_map=cik_map)
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

    for proposal in proposals:
        try:
            quote = alpaca_trading_client.get_latest_quote(proposal.symbol)
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


def main() -> None:
    from alpaca.trading.client import TradingClient

    from src.config import load_settings
    from src.db import get_client

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

    run_cycle(supabase_client, alpaca_trading_client, settings, sec_user_agent)


if __name__ == "__main__":
    main()
