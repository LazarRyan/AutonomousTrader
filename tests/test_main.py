import pytest

from src.agents.execution import ExecutionRequest, ExecutionResult
from src.agents.portfolio_manager import CandidateTradeProposal
from src.main import is_trading_halted, process_candidate_trade
from src.risk.safety_rails import SafetyState
from src.risk.scorer import RiskScorerConfig


class TestIsTradingHalted:
    def test_clean_state_is_not_halted(self):
        halted, reason = is_trading_halted(SafetyState())
        assert halted is False
        assert reason == "trading is active"

    def test_kill_switch_halts(self):
        halted, reason = is_trading_halted(SafetyState(kill_switch_engaged=True))
        assert halted is True
        assert "kill switch" in reason

    def test_daily_halt_halts(self):
        halted, reason = is_trading_halted(SafetyState(daily_halted=True))
        assert halted is True
        assert "daily loss limit" in reason

    def test_weekly_halt_halts(self):
        halted, reason = is_trading_halted(SafetyState(weekly_halted=True))
        assert halted is True
        assert "weekly loss limit" in reason

    def test_multiple_reasons_all_reported(self):
        halted, reason = is_trading_halted(
            SafetyState(kill_switch_engaged=True, daily_halted=True, weekly_halted=True)
        )
        assert halted is True
        assert "kill switch" in reason
        assert "daily loss limit" in reason
        assert "weekly loss limit" in reason


class FakeCycleDeps:
    def __init__(self, execute_result: ExecutionResult | None = None):
        self.candidate_trade_inserts = []
        self.approval_queue_inserts = []
        self.log_audit_calls = []
        self.execute_trade_calls = []
        self._next_id = 1
        self._execute_result = execute_result or ExecutionResult(
            status="executed", reasoning="ok", alpaca_order_id="oid-1"
        )

    def insert_candidate_trade(self, proposal, proposed_price, risk_result):
        candidate_trade_id = f"ct-{self._next_id}"
        self._next_id += 1
        self.candidate_trade_inserts.append((proposal, proposed_price, risk_result, candidate_trade_id))
        return candidate_trade_id

    def insert_approval_queue_item(self, candidate_trade_id, risk_result):
        self.approval_queue_inserts.append((candidate_trade_id, risk_result))

    def log_audit(self, event_type, decision, reasoning, symbol=None, candidate_trade_id=None, metadata=None):
        self.log_audit_calls.append(dict(event_type=event_type, decision=decision, reasoning=reasoning))

    def execute_trade_fn(self, request: ExecutionRequest) -> ExecutionResult:
        self.execute_trade_calls.append(request)
        return self._execute_result


def make_proposal(**overrides) -> CandidateTradeProposal:
    defaults = dict(symbol="AAPL", side="buy", quantity=10.0, reasoning="strong momentum")
    defaults.update(overrides)
    return CandidateTradeProposal(**defaults)


class TestProcessCandidateTrade:
    def test_low_risk_trade_is_auto_executed_not_queued(self):
        deps = FakeCycleDeps()
        outcome = process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,  # trade value $100 on a $100k portfolio -- tiny, low vol/liquidity penalty
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.01,
            benchmark_30d_volatility=0.01,
            liquidity_penalty=0.0,
            insert_candidate_trade=deps.insert_candidate_trade,
            insert_approval_queue_item=deps.insert_approval_queue_item,
            execute_trade_fn=deps.execute_trade_fn,
            log_audit=deps.log_audit,
        )
        assert outcome == "executed"
        assert deps.approval_queue_inserts == []
        assert len(deps.execute_trade_calls) == 1
        assert len(deps.candidate_trade_inserts) == 1

    def test_high_risk_trade_is_queued_not_executed(self):
        # Note: with the default risk-scorer weights, a tiny position (as
        # used here to isolate the volatility/liquidity components) can't
        # reach the default 70-point threshold on its own -- see the
        # "KNOWN TENSION" note in risk/scorer.py. Use a lower threshold here
        # to exercise the composite-score path specifically, independent of
        # the 5% hard override tested separately below.
        deps = FakeCycleDeps()
        outcome = process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.20,  # 10x benchmark -> capped, high vol component
            benchmark_30d_volatility=0.02,
            liquidity_penalty=100.0,  # max illiquidity penalty
            insert_candidate_trade=deps.insert_candidate_trade,
            insert_approval_queue_item=deps.insert_approval_queue_item,
            execute_trade_fn=deps.execute_trade_fn,
            log_audit=deps.log_audit,
            risk_config=RiskScorerConfig(approval_threshold=40.0),
        )
        assert outcome == "queued_for_approval"
        assert len(deps.approval_queue_inserts) == 1
        assert deps.execute_trade_calls == []

    def test_hard_override_position_size_queues_even_with_benign_inputs(self):
        deps = FakeCycleDeps()
        # 6% of portfolio -- hard override regardless of otherwise-clean signal
        outcome = process_candidate_trade(
            make_proposal(quantity=60.0),
            proposed_price=100.0,  # $6,000 / $100,000 = 6%
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.01,
            benchmark_30d_volatility=0.01,
            liquidity_penalty=0.0,
            insert_candidate_trade=deps.insert_candidate_trade,
            insert_approval_queue_item=deps.insert_approval_queue_item,
            execute_trade_fn=deps.execute_trade_fn,
            log_audit=deps.log_audit,
        )
        assert outcome == "queued_for_approval"
        assert deps.execute_trade_calls == []

    def test_candidate_trade_is_always_persisted_regardless_of_outcome(self):
        deps_auto = FakeCycleDeps()
        process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.01,
            benchmark_30d_volatility=0.01,
            liquidity_penalty=0.0,
            insert_candidate_trade=deps_auto.insert_candidate_trade,
            insert_approval_queue_item=deps_auto.insert_approval_queue_item,
            execute_trade_fn=deps_auto.execute_trade_fn,
            log_audit=deps_auto.log_audit,
        )
        assert len(deps_auto.candidate_trade_inserts) == 1

        deps_queued = FakeCycleDeps()
        process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.20,
            benchmark_30d_volatility=0.02,
            liquidity_penalty=100.0,
            insert_candidate_trade=deps_queued.insert_candidate_trade,
            insert_approval_queue_item=deps_queued.insert_approval_queue_item,
            execute_trade_fn=deps_queued.execute_trade_fn,
            log_audit=deps_queued.log_audit,
        )
        assert len(deps_queued.candidate_trade_inserts) == 1

    def test_audit_log_reflects_auto_approved_vs_queued(self):
        deps_auto = FakeCycleDeps()
        process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.01,
            benchmark_30d_volatility=0.01,
            liquidity_penalty=0.0,
            insert_candidate_trade=deps_auto.insert_candidate_trade,
            insert_approval_queue_item=deps_auto.insert_approval_queue_item,
            execute_trade_fn=deps_auto.execute_trade_fn,
            log_audit=deps_auto.log_audit,
        )
        assert deps_auto.log_audit_calls[0]["decision"] == "auto_approved"

        deps_queued = FakeCycleDeps()
        process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.20,
            benchmark_30d_volatility=0.02,
            liquidity_penalty=100.0,
            insert_candidate_trade=deps_queued.insert_candidate_trade,
            insert_approval_queue_item=deps_queued.insert_approval_queue_item,
            execute_trade_fn=deps_queued.execute_trade_fn,
            log_audit=deps_queued.log_audit,
            risk_config=RiskScorerConfig(approval_threshold=40.0),
        )
        assert deps_queued.log_audit_calls[0]["decision"] == "queued_for_approval"

    def test_execution_outcome_is_surfaced_even_after_auto_approval(self):
        # Auto-approved by the risk scorer doesn't guarantee execution --
        # the Execution Agent's own safety rails can still block it.
        deps = FakeCycleDeps(execute_result=ExecutionResult(status="blocked", reasoning="kill switch engaged"))
        outcome = process_candidate_trade(
            make_proposal(quantity=1.0),
            proposed_price=100.0,
            total_portfolio_value=100_000.0,
            asset_30d_volatility=0.01,
            benchmark_30d_volatility=0.01,
            liquidity_penalty=0.0,
            insert_candidate_trade=deps.insert_candidate_trade,
            insert_approval_queue_item=deps.insert_approval_queue_item,
            execute_trade_fn=deps.execute_trade_fn,
            log_audit=deps.log_audit,
        )
        assert outcome == "blocked"
