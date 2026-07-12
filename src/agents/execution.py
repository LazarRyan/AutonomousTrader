"""
Execution Agent -- the ONLY module allowed to place a trade order.

This is the most safety-critical file in the codebase: it's the single
choke point between a proposed trade and real (paper) money moving. Unlike
the other network-touching modules in this project (momentum.py's
fetch_daily_closes, insider_edgar.py's fetch_*, etc.), which are thin
wrappers left untested because they just move data, this module's CONTROL
FLOW is fully unit-tested via dependency injection -- every gate (trading
mode, safety rails, order placement, failure handling) has a test proving
it actually blocks/proceeds correctly. Only the literal Alpaca API call
inside place_alpaca_order() is untested (needs live network + real keys).

Every call path through execute_trade(), in order, no exceptions:

  1. Refuse immediately unless trading_mode == "paper". There is no live
     trading code path in this codebase at all -- TRADING_MODE=live is
     unreachable by construction, not just discouraged. This check happens
     BEFORE the safety rail check, so a misconfigured live mode can never
     even reach the point of asking "is this trade safe" -- it's refused on
     mode alone.
  2. Load-fresh safety rails check (risk.safety_rails.evaluate_trade) --
     kill switch, daily/weekly loss halts, max position size. A trade that
     already has human approval from the CLI watcher still has to clear
     this. Nothing here can be bypassed by an upstream approval.
  3. Place the order (paper) via the injected place_order callable.
  4. Record the outcome: audit_log always, executed_trades on success,
     candidate_trades.status updated in every branch (executed / blocked /
     execution_failed / refused_live_mode) so nothing is left ambiguous.

Dependencies (place_order, log_audit, record_executed_trade,
update_candidate_trade_status) are passed in as plain callables rather than
this module constructing its own Supabase/Alpaca clients internally. That's
what makes 1-4 above fully unit-testable with simple fakes -- see
tests/test_execution.py. Real wiring (the actual Supabase/Alpaca calls)
lives in make_live_dependencies() at the bottom of this file, which is
itself thin and untested, same as the rest of this project's network glue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from src.risk.safety_rails import SafetyConfig, SafetyState, evaluate_trade

PlaceOrderFn = Callable[[str, str, float], str]  # (symbol, side, quantity) -> alpaca_order_id
LogAuditFn = Callable[..., None]  # (event_type, decision, reasoning, symbol=, candidate_trade_id=, metadata=)
RecordExecutedTradeFn = Callable[..., None]  # (candidate_trade_id, alpaca_order_id, symbol, side, quantity)
UpdateStatusFn = Callable[[str, str], None]  # (candidate_trade_id, status)


@dataclass(frozen=True)
class ExecutionRequest:
    candidate_trade_id: str
    symbol: str
    side: str  # "buy" or "sell"
    quantity: float
    trade_value: float  # dollar value of this trade, for the safety-rail size check
    total_portfolio_value: float
    # Cash available for buys, for the no-margin safety rail (see
    # risk/safety_rails.py -- added after the account's cash went negative
    # in practice). None skips that rail; both real callers (run_cycle and
    # the approval watcher) always supply a value, the default exists only
    # so the field is non-breaking for older call sites.
    cash_available: float | None = None

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if self.trade_value < 0:
            raise ValueError(f"trade_value must be >= 0, got {self.trade_value}")
        if self.total_portfolio_value <= 0:
            raise ValueError(f"total_portfolio_value must be > 0, got {self.total_portfolio_value}")


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # "executed", "blocked", "refused_live_mode", "execution_failed"
    reasoning: str
    alpaca_order_id: str | None = None


def execute_trade(
    request: ExecutionRequest,
    trading_mode: str,
    safety_state: SafetyState,
    place_order: PlaceOrderFn,
    log_audit: LogAuditFn,
    record_executed_trade: RecordExecutedTradeFn,
    update_candidate_trade_status: UpdateStatusFn,
    safety_config: SafetyConfig | None = None,
) -> ExecutionResult:
    safety_config = safety_config or SafetyConfig()

    # --- Gate 1: trading mode. Checked before anything else, including the
    # safety rails, so a misconfigured live mode is refused on principle
    # alone, not because a rail happened to catch it.
    if trading_mode != "paper":
        reasoning = (
            f"trading_mode={trading_mode!r} has no execution code path -- only 'paper' is "
            f"supported until live trading is built as its own explicit, separate phase"
        )
        log_audit(
            event_type="execution_attempt",
            decision="refused_live_mode",
            reasoning=reasoning,
            symbol=request.symbol,
            candidate_trade_id=request.candidate_trade_id,
        )
        update_candidate_trade_status(request.candidate_trade_id, "blocked")
        return ExecutionResult(status="refused_live_mode", reasoning=reasoning)

    # --- Gate 2: safety rails. Independent of and stricter than any
    # upstream risk-score approval -- see risk/safety_rails.py.
    safety_decision = evaluate_trade(
        request.trade_value,
        request.total_portfolio_value,
        safety_state,
        safety_config,
        side=request.side,
        cash_available=request.cash_available,
    )
    if not safety_decision.allowed:
        log_audit(
            event_type="execution_attempt",
            decision="blocked",
            reasoning=safety_decision.reasoning,
            symbol=request.symbol,
            candidate_trade_id=request.candidate_trade_id,
        )
        update_candidate_trade_status(request.candidate_trade_id, "blocked")
        return ExecutionResult(status="blocked", reasoning=safety_decision.reasoning)

    # --- Gate 3: place the order. Any failure here is caught, logged, and
    # surfaced as a distinct status -- never silently swallowed, and never
    # confused with a safety-rail block (that's a "by design" outcome; this
    # is "something broke and needs attention").
    try:
        alpaca_order_id = place_order(request.symbol, request.side, request.quantity)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure must be caught and logged
        reasoning = f"order placement failed: {exc}"
        log_audit(
            event_type="execution_attempt",
            decision="execution_failed",
            reasoning=reasoning,
            symbol=request.symbol,
            candidate_trade_id=request.candidate_trade_id,
        )
        update_candidate_trade_status(request.candidate_trade_id, "execution_failed")
        return ExecutionResult(status="execution_failed", reasoning=reasoning)

    # --- Success.
    record_executed_trade(
        candidate_trade_id=request.candidate_trade_id,
        alpaca_order_id=alpaca_order_id,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
    )
    reasoning = f"order placed successfully (paper), alpaca_order_id={alpaca_order_id}"
    log_audit(
        event_type="execution_attempt",
        decision="executed",
        reasoning=reasoning,
        symbol=request.symbol,
        candidate_trade_id=request.candidate_trade_id,
        metadata={"alpaca_order_id": alpaca_order_id},
    )
    update_candidate_trade_status(request.candidate_trade_id, "executed")
    return ExecutionResult(status="executed", reasoning=reasoning, alpaca_order_id=alpaca_order_id)


# ============================================================
# Live wiring -- not unit-tested here (requires a live Alpaca trading
# client and a live Supabase client). Thin glue only.
# ============================================================


class _TradingClientLike(Protocol):
    def submit_order(self, order_data) -> object: ...  # matches alpaca-py's TradingClient.submit_order


def place_alpaca_order(symbol: str, side: str, quantity: float, trading_client: _TradingClientLike) -> str:
    """Real Alpaca order placement (paper trading endpoint only -- the
    trading_client passed in must itself be constructed against Alpaca's
    paper base URL; this function does not and cannot verify that from
    here, which is exactly why the trading_mode check in execute_trade()
    happens in this codebase rather than being delegated to Alpaca."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    order_request = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = trading_client.submit_order(order_request)
    return str(order.id)


def make_live_dependencies(supabase_client, alpaca_trading_client):
    """Build the four execute_trade() dependencies against real Supabase +
    Alpaca clients. Returns (place_order, log_audit, record_executed_trade,
    update_candidate_trade_status) ready to pass into execute_trade().
    """
    from src.db import write_audit_log

    def place_order(symbol: str, side: str, quantity: float) -> str:
        return place_alpaca_order(symbol, side, quantity, alpaca_trading_client)

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

    def record_executed_trade(candidate_trade_id, alpaca_order_id, symbol, side, quantity):
        supabase_client.table("executed_trades").insert(
            {
                "candidate_trade_id": candidate_trade_id,
                "alpaca_order_id": alpaca_order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
            }
        ).execute()

    def update_candidate_trade_status(candidate_trade_id, status):
        supabase_client.table("candidate_trades").update({"status": status}).eq(
            "id", candidate_trade_id
        ).execute()

    return place_order, log_audit, record_executed_trade, update_candidate_trade_status
