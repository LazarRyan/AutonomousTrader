"""
Full control-flow coverage of the Execution Agent using fake injected
dependencies -- no network, no real Alpaca/Supabase clients. This is the
one network-adjacent module in the project that gets full test coverage of
its logic (not just its pure helper functions), because it's the only
place a real order gets placed.
"""

import pytest

from src.agents.execution import ExecutionRequest, execute_trade
from src.risk.safety_rails import SafetyConfig, SafetyState


class FakeDependencies:
    """Records every call made to it so tests can assert on exactly what
    happened, and controls whether place_order succeeds or raises."""

    def __init__(self, place_order_should_fail: bool = False, order_id: str = "order-123"):
        self.place_order_calls = []
        self.log_audit_calls = []
        self.record_executed_trade_calls = []
        self.update_status_calls = []
        self._place_order_should_fail = place_order_should_fail
        self._order_id = order_id

    def place_order(self, symbol: str, side: str, quantity: float) -> str:
        self.place_order_calls.append((symbol, side, quantity))
        if self._place_order_should_fail:
            raise RuntimeError("simulated Alpaca API failure")
        return self._order_id

    def log_audit(self, event_type, decision, reasoning, symbol=None, candidate_trade_id=None, metadata=None):
        self.log_audit_calls.append(
            dict(
                event_type=event_type,
                decision=decision,
                reasoning=reasoning,
                symbol=symbol,
                candidate_trade_id=candidate_trade_id,
                metadata=metadata,
            )
        )

    def record_executed_trade(self, candidate_trade_id, alpaca_order_id, symbol, side, quantity):
        self.record_executed_trade_calls.append(
            dict(
                candidate_trade_id=candidate_trade_id,
                alpaca_order_id=alpaca_order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
            )
        )

    def update_candidate_trade_status(self, candidate_trade_id, status):
        self.update_status_calls.append((candidate_trade_id, status))


def make_request(**overrides) -> ExecutionRequest:
    defaults = dict(
        candidate_trade_id="ct-1",
        symbol="AAPL",
        side="buy",
        quantity=10.0,
        trade_value=2_000.0,
        total_portfolio_value=100_000.0,
    )
    defaults.update(overrides)
    return ExecutionRequest(**defaults)


def run(request, trading_mode="paper", safety_state=None, safety_config=None, deps=None):
    deps = deps or FakeDependencies()
    safety_state = safety_state or SafetyState()
    result = execute_trade(
        request,
        trading_mode=trading_mode,
        safety_state=safety_state,
        place_order=deps.place_order,
        log_audit=deps.log_audit,
        record_executed_trade=deps.record_executed_trade,
        update_candidate_trade_status=deps.update_candidate_trade_status,
        safety_config=safety_config,
    )
    return result, deps


class TestExecutionRequestValidation:
    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            make_request(side="hold")

    def test_nonpositive_quantity_raises(self):
        with pytest.raises(ValueError):
            make_request(quantity=0)

    def test_negative_trade_value_raises(self):
        with pytest.raises(ValueError):
            make_request(trade_value=-1)

    def test_nonpositive_portfolio_value_raises(self):
        with pytest.raises(ValueError):
            make_request(total_portfolio_value=0)


class TestTradingModeGate:
    def test_live_mode_is_refused_before_touching_anything_else(self):
        result, deps = run(make_request(), trading_mode="live")
        assert result.status == "refused_live_mode"
        assert deps.place_order_calls == []
        assert deps.record_executed_trade_calls == []
        assert deps.update_status_calls == [("ct-1", "blocked")]
        assert deps.log_audit_calls[0]["decision"] == "refused_live_mode"

    def test_unknown_mode_is_also_refused(self):
        result, deps = run(make_request(), trading_mode="sandbox")
        assert result.status == "refused_live_mode"
        assert deps.place_order_calls == []

    def test_live_mode_refused_even_if_safety_state_is_perfectly_clean(self):
        # Proves mode is checked BEFORE safety rails, not as a fallback.
        clean_state = SafetyState()
        result, deps = run(make_request(), trading_mode="live", safety_state=clean_state)
        assert result.status == "refused_live_mode"


class TestSafetyRailGate:
    def test_kill_switch_blocks_and_never_places_order(self):
        state = SafetyState(kill_switch_engaged=True)
        result, deps = run(make_request(), safety_state=state)
        assert result.status == "blocked"
        assert deps.place_order_calls == []
        assert deps.update_status_calls == [("ct-1", "blocked")]
        assert "kill_switch" in deps.log_audit_calls[0]["reasoning"]

    def test_daily_halt_blocks(self):
        config = SafetyConfig(max_daily_loss_pct=0.03)
        state = SafetyState()
        state.record_pnl(daily_pnl_pct=-0.05, weekly_pnl_pct=-0.01, config=config)
        result, deps = run(make_request(), safety_state=state, safety_config=config)
        assert result.status == "blocked"
        assert deps.place_order_calls == []

    def test_oversized_position_blocks(self):
        config = SafetyConfig(max_position_pct=0.05)
        request = make_request(trade_value=10_000, total_portfolio_value=100_000)  # 10% > 5%
        result, deps = run(request, safety_config=config)
        assert result.status == "blocked"
        assert deps.place_order_calls == []

    def test_clean_safety_state_proceeds_to_place_order(self):
        result, deps = run(make_request())
        assert result.status == "executed"
        assert len(deps.place_order_calls) == 1


class TestOrderPlacement:
    def test_successful_order_records_execution_and_updates_status(self):
        deps = FakeDependencies(order_id="alpaca-order-999")
        result, deps = run(make_request(symbol="MSFT", side="sell", quantity=5.0), deps=deps)

        assert result.status == "executed"
        assert result.alpaca_order_id == "alpaca-order-999"
        assert deps.place_order_calls == [("MSFT", "sell", 5.0)]
        assert deps.record_executed_trade_calls[0]["alpaca_order_id"] == "alpaca-order-999"
        assert deps.record_executed_trade_calls[0]["symbol"] == "MSFT"
        assert deps.update_status_calls == [("ct-1", "executed")]
        assert deps.log_audit_calls[0]["decision"] == "executed"

    def test_place_order_failure_is_caught_logged_and_marked_execution_failed(self):
        deps = FakeDependencies(place_order_should_fail=True)
        result, deps = run(make_request(), deps=deps)

        assert result.status == "execution_failed"
        assert "simulated Alpaca API failure" in result.reasoning
        assert deps.record_executed_trade_calls == []
        assert deps.update_status_calls == [("ct-1", "execution_failed")]
        assert deps.log_audit_calls[0]["decision"] == "execution_failed"

    def test_execution_failure_is_never_confused_with_safety_block(self):
        # A blocked trade and a failed trade must produce distinct statuses
        # so the audit trail can distinguish "by design" from "something broke".
        blocked_result, _ = run(make_request(), safety_state=SafetyState(kill_switch_engaged=True))
        failed_result, _ = run(make_request(), deps=FakeDependencies(place_order_should_fail=True))
        assert blocked_result.status != failed_result.status
        assert blocked_result.status == "blocked"
        assert failed_result.status == "execution_failed"
