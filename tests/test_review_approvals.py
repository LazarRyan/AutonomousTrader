import pytest

from scripts.review_approvals import (
    ApprovalItem,
    format_approval_prompt,
    handle_approval_decision,
    prompt_yes_no,
)
from src.agents.execution import ExecutionRequest, ExecutionResult


def make_item(**overrides) -> ApprovalItem:
    defaults = dict(
        approval_id="appr-1",
        candidate_trade_id="ct-1",
        symbol="AAPL",
        side="buy",
        quantity=100.0,
        proposed_price=150.0,
        risk_score=82.5,
        risk_breakdown={"size_component": 60.0, "volatility_component": 90.0},
        approval_reasoning="composite score 82.5 >= approval threshold 70.0",
        portfolio_manager_reasoning="Strong bullish blended signal, no existing position.",
        created_at="2026-07-03T14:00:00Z",
    )
    defaults.update(overrides)
    return ApprovalItem(**defaults)


class TestFormatApprovalPrompt:
    def test_includes_key_fields(self):
        text = format_approval_prompt(make_item())
        assert "BUY 100 AAPL" in text
        assert "$150.00" in text
        assert "15,000.00" in text  # trade value
        assert "82.5" in text
        assert "composite score 82.5" in text
        assert "Strong bullish" in text

    def test_handles_missing_price(self):
        text = format_approval_prompt(make_item(proposed_price=None))
        assert "UNKNOWN" in text

    def test_handles_missing_portfolio_manager_reasoning(self):
        text = format_approval_prompt(make_item(portfolio_manager_reasoning=None))
        assert "Portfolio manager reasoning" not in text


class TestPromptYesNo:
    def test_accepts_y(self):
        assert prompt_yes_no("prompt", input_fn=lambda _: "y") is True

    def test_accepts_yes(self):
        assert prompt_yes_no("prompt", input_fn=lambda _: "yes") is True

    def test_accepts_n(self):
        assert prompt_yes_no("prompt", input_fn=lambda _: "n") is False

    def test_is_case_insensitive(self):
        assert prompt_yes_no("prompt", input_fn=lambda _: "Y") is True

    def test_reprompts_on_invalid_input(self):
        responses = iter(["maybe", "definitely not", "n"])
        result = prompt_yes_no("prompt", input_fn=lambda _: next(responses))
        assert result is False


class FakeDecisionDeps:
    def __init__(self, execute_result: ExecutionResult | None = None):
        self.approval_status_calls = []
        self.candidate_status_calls = []
        self.log_audit_calls = []
        self.execute_trade_calls = []
        self._execute_result = execute_result or ExecutionResult(status="executed", reasoning="ok", alpaca_order_id="oid-1")

    def update_approval_status(self, approval_id, status, resolved_by):
        self.approval_status_calls.append((approval_id, status, resolved_by))

    def update_candidate_trade_status(self, candidate_trade_id, status):
        self.candidate_status_calls.append((candidate_trade_id, status))

    def log_audit(self, event_type, decision, reasoning, symbol=None, candidate_trade_id=None, metadata=None):
        self.log_audit_calls.append(dict(event_type=event_type, decision=decision, reasoning=reasoning))

    def execute_trade_fn(self, request: ExecutionRequest) -> ExecutionResult:
        self.execute_trade_calls.append(request)
        return self._execute_result


class TestHandleApprovalDecision:
    def test_rejection_updates_statuses_and_never_calls_execute(self):
        deps = FakeDecisionDeps()
        outcome = handle_approval_decision(
            make_item(),
            approved=False,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps.execute_trade_fn,
            update_approval_status=deps.update_approval_status,
            update_candidate_trade_status=deps.update_candidate_trade_status,
            log_audit=deps.log_audit,
        )
        assert outcome == "rejected"
        assert deps.approval_status_calls == [("appr-1", "rejected", "ryan_cli")]
        assert deps.candidate_status_calls == [("ct-1", "rejected")]
        assert deps.execute_trade_calls == []

    def test_approval_marks_approval_queue_and_calls_execute_with_correct_trade_value(self):
        deps = FakeDecisionDeps()
        outcome = handle_approval_decision(
            make_item(quantity=100.0, proposed_price=150.0),
            approved=True,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps.execute_trade_fn,
            update_approval_status=deps.update_approval_status,
            update_candidate_trade_status=deps.update_candidate_trade_status,
            log_audit=deps.log_audit,
        )
        assert outcome == "executed"
        assert deps.approval_status_calls == [("appr-1", "approved", "ryan_cli")]
        assert len(deps.execute_trade_calls) == 1
        request = deps.execute_trade_calls[0]
        assert request.trade_value == pytest.approx(15_000.0)
        assert request.symbol == "AAPL"
        assert request.side == "buy"
        assert request.total_portfolio_value == 100_000.0

    def test_approval_without_price_is_blocked_not_guessed(self):
        deps = FakeDecisionDeps()
        outcome = handle_approval_decision(
            make_item(proposed_price=None),
            approved=True,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps.execute_trade_fn,
            update_approval_status=deps.update_approval_status,
            update_candidate_trade_status=deps.update_candidate_trade_status,
            log_audit=deps.log_audit,
        )
        assert outcome == "blocked_missing_price"
        assert deps.execute_trade_calls == []
        assert deps.candidate_status_calls == [("ct-1", "blocked")]

    def test_approval_result_reflects_execution_agent_outcome(self):
        # Even after human approval, the Execution Agent's own safety-rail
        # gate can still block -- handle_approval_decision must surface
        # that outcome, not silently report "approved" as the final status.
        deps = FakeDecisionDeps(
            execute_result=ExecutionResult(status="blocked", reasoning="kill_switch: kill switch is engaged")
        )
        outcome = handle_approval_decision(
            make_item(),
            approved=True,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps.execute_trade_fn,
            update_approval_status=deps.update_approval_status,
            update_candidate_trade_status=deps.update_candidate_trade_status,
            log_audit=deps.log_audit,
        )
        assert outcome == "blocked"

    def test_audit_log_written_for_both_approval_and_rejection(self):
        deps = FakeDecisionDeps()
        handle_approval_decision(
            make_item(),
            approved=False,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps.execute_trade_fn,
            update_approval_status=deps.update_approval_status,
            update_candidate_trade_status=deps.update_candidate_trade_status,
            log_audit=deps.log_audit,
        )
        assert deps.log_audit_calls[0]["decision"] == "rejected"

        deps2 = FakeDecisionDeps()
        handle_approval_decision(
            make_item(),
            approved=True,
            total_portfolio_value=100_000.0,
            execute_trade_fn=deps2.execute_trade_fn,
            update_approval_status=deps2.update_approval_status,
            update_candidate_trade_status=deps2.update_candidate_trade_status,
            log_audit=deps2.log_audit,
        )
        assert deps2.log_audit_calls[0]["decision"] == "approved"
