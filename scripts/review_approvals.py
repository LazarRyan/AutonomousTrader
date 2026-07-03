"""
CLI approval watcher -- the actual approval mechanism (build plan 4b).

Meant to run continuously (this is designed to be left running 24/7 in a
terminal tab, tmux session, or as a launchd/systemd service): it polls the
`approval_queue` table in Supabase, and the moment a high-risk trade lands,
prints the trade plus full reasoning (signals, risk score, why it crossed
the threshold) and blocks on a y/n right there in the terminal. Nothing
executes until answered. There is no timeout and no auto-fire -- if this
script isn't running, queued trades just wait; that's the safe failure
mode, not an error condition. Email notification (not yet built) can fire
in parallel as a backup heads-up, but this CLI is the actual approval gate.

Same split as the rest of the project:

  1. PURE, unit-tested logic, no network/no blocking input:
       - format_approval_prompt(): deterministic display formatting.
       - handle_approval_decision(): what happens after a y/n is given --
         update statuses, log the audit trail, and (if approved) hand off
         to the Execution Agent. Fully testable via injected fake
         dependencies, same dependency-injection pattern as
         agents/execution.py -- this is the second-most safety-critical
         function in the project (it's what turns a human's "yes" into an
         actual order), so it gets full control-flow test coverage.
       - prompt_yes_no(): blocking input loop, but with an injectable
         input function so it's testable without real stdin.

  2. Thin network wrappers (fetch_pending_approvals, update_approval_status,
     get_portfolio_value) and the main poll loop (poll_approval_queue): not
     unit-tested -- real Supabase/Alpaca calls and an infinite loop.

Run with: python scripts/review_approvals.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, ".")  # allow `python scripts/review_approvals.py` from repo root

from src.agents.execution import ExecutionRequest, ExecutionResult

LogAuditFn = Callable[..., None]
UpdateApprovalStatusFn = Callable[[str, str, str], None]  # (approval_id, status, resolved_by)
UpdateCandidateStatusFn = Callable[[str, str], None]  # (candidate_trade_id, status)
ExecuteTradeFn = Callable[[ExecutionRequest], ExecutionResult]

RESOLVED_BY_CLI = "ryan_cli"

DEFAULT_POLL_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class ApprovalItem:
    approval_id: str
    candidate_trade_id: str
    symbol: str
    side: str
    quantity: float
    proposed_price: float | None
    risk_score: float | None
    risk_breakdown: dict
    approval_reasoning: str  # approval_queue.reasoning -- why this needed a human
    portfolio_manager_reasoning: str | None
    created_at: str


def format_approval_prompt(item: ApprovalItem) -> str:
    """Deterministic display formatting. Pure function -- fully unit-tested."""
    lines = [
        "=" * 64,
        f"APPROVAL NEEDED: {item.side.upper()} {item.quantity:g} {item.symbol}",
        "=" * 64,
    ]

    if item.proposed_price is not None:
        trade_value = item.quantity * item.proposed_price
        lines.append(f"Proposed price: ${item.proposed_price:,.2f}  (trade value ~${trade_value:,.2f})")
    else:
        lines.append("Proposed price: UNKNOWN -- trade value cannot be computed")

    if item.risk_score is not None:
        lines.append(f"Risk score: {item.risk_score:.1f}")
    if item.risk_breakdown:
        breakdown_str = ", ".join(f"{k}={v}" for k, v in sorted(item.risk_breakdown.items()))
        lines.append(f"Risk breakdown: {breakdown_str}")

    lines.append(f"Why this needs approval: {item.approval_reasoning}")

    if item.portfolio_manager_reasoning:
        lines.append(f"Portfolio manager reasoning: {item.portfolio_manager_reasoning}")

    lines.append(f"Filed: {item.created_at}")
    lines.append("-" * 64)

    return "\n".join(lines)


def prompt_yes_no(prompt_text: str, input_fn: Callable[[str], str] = input) -> bool:
    """Blocks until a valid y/n is given. input_fn is injectable so this is
    testable without real stdin."""
    while True:
        response = input_fn(prompt_text).strip().lower()
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def handle_approval_decision(
    item: ApprovalItem,
    approved: bool,
    total_portfolio_value: float,
    execute_trade_fn: ExecuteTradeFn,
    update_approval_status: UpdateApprovalStatusFn,
    update_candidate_trade_status: UpdateCandidateStatusFn,
    log_audit: LogAuditFn,
) -> str:
    """What happens after a y/n is given. Returns the final outcome status
    string. Fully unit-tested via fake dependencies -- see
    tests/test_review_approvals.py.
    """
    if not approved:
        update_approval_status(item.approval_id, "rejected", RESOLVED_BY_CLI)
        update_candidate_trade_status(item.candidate_trade_id, "rejected")
        log_audit(
            event_type="approval_decision",
            decision="rejected",
            reasoning="rejected via CLI approval watcher",
            symbol=item.symbol,
            candidate_trade_id=item.candidate_trade_id,
        )
        return "rejected"

    update_approval_status(item.approval_id, "approved", RESOLVED_BY_CLI)
    log_audit(
        event_type="approval_decision",
        decision="approved",
        reasoning="approved via CLI approval watcher",
        symbol=item.symbol,
        candidate_trade_id=item.candidate_trade_id,
    )

    if item.proposed_price is None:
        # Approved by the human, but we don't have a price to compute trade
        # value from -- refuse to guess. Block and flag for manual review
        # rather than execute with an unknown size.
        update_candidate_trade_status(item.candidate_trade_id, "blocked")
        log_audit(
            event_type="approval_decision",
            decision="blocked",
            reasoning=(
                "approved by human but candidate_trades.proposed_price is missing -- cannot "
                "compute trade value safely, refusing to execute rather than guess"
            ),
            symbol=item.symbol,
            candidate_trade_id=item.candidate_trade_id,
        )
        return "blocked_missing_price"

    trade_value = item.quantity * item.proposed_price
    request = ExecutionRequest(
        candidate_trade_id=item.candidate_trade_id,
        symbol=item.symbol,
        side=item.side,
        quantity=item.quantity,
        trade_value=trade_value,
        total_portfolio_value=total_portfolio_value,
    )
    # Note: execute_trade_fn still runs the trade through the safety rails
    # (kill switch, loss halts, max position size) -- a human's "yes" here
    # approves the RISK SCORE gate only, not the safety rails. See
    # agents/execution.py.
    result = execute_trade_fn(request)
    return result.status


# ============================================================
# Network wrappers + main loop -- not unit-tested here (real Supabase/Alpaca
# calls and an infinite polling loop).
# ============================================================


def fetch_pending_approvals(supabase_client) -> list[ApprovalItem]:
    response = (
        supabase_client.table("approval_queue")
        .select("*, candidate_trades(*)")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )

    items = []
    for row in response.data:
        candidate = row.get("candidate_trades") or {}
        items.append(
            ApprovalItem(
                approval_id=row["id"],
                candidate_trade_id=row["candidate_trade_id"],
                symbol=candidate.get("symbol", "UNKNOWN"),
                side=candidate.get("side", "unknown"),
                quantity=candidate.get("quantity", 0.0),
                proposed_price=candidate.get("proposed_price"),
                risk_score=row.get("risk_score"),
                risk_breakdown=candidate.get("risk_breakdown") or {},
                approval_reasoning=row.get("reasoning") or "",
                portfolio_manager_reasoning=candidate.get("portfolio_manager_reasoning"),
                created_at=row.get("created_at", ""),
            )
        )
    return items


def update_approval_status(supabase_client, approval_id: str, status: str, resolved_by: str) -> None:
    from datetime import datetime, timezone

    supabase_client.table("approval_queue").update(
        {
            "status": status,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": resolved_by,
        }
    ).eq("id", approval_id).execute()


def get_portfolio_value(alpaca_trading_client) -> float:
    account = alpaca_trading_client.get_account()
    return float(account.equity)


def poll_approval_queue(
    supabase_client,
    alpaca_trading_client,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """The main 24/7 loop. Never raises out of the loop body on a per-item
    or per-poll basis -- transient errors are printed and the loop
    continues, since this is meant to stay up indefinitely. A real
    KeyboardInterrupt (Ctrl+C) is the only intended way to stop it.
    """
    from src.agents.execution import execute_trade
    from src.agents.execution import make_live_dependencies
    from src.config import load_settings
    from src.db import get_client, write_audit_log
    from src.risk.safety_rails import SafetyConfig, SafetyState

    settings = load_settings()

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

    place_order, _log_audit_unused, record_executed_trade, update_candidate_trade_status = (
        make_live_dependencies(supabase_client, alpaca_trading_client)
    )

    print(f"Approval watcher started. Polling every {poll_interval_seconds}s. Ctrl+C to stop.")

    while True:
        try:
            items = fetch_pending_approvals(supabase_client)
            for item in items:
                print(format_approval_prompt(item))
                approved = prompt_yes_no("Approve this trade? [y/n]: ")

                total_portfolio_value = get_portfolio_value(alpaca_trading_client)

                safety_row = supabase_client.table("safety_state").select("*").limit(1).execute().data[0]
                safety_state = SafetyState(
                    kill_switch_engaged=safety_row["kill_switch_engaged"],
                    daily_pnl_pct=safety_row["daily_pnl_pct"],
                    weekly_pnl_pct=safety_row["weekly_pnl_pct"],
                    daily_halted=safety_row["daily_halted"],
                    weekly_halted=safety_row["weekly_halted"],
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

                outcome = handle_approval_decision(
                    item,
                    approved=approved,
                    total_portfolio_value=total_portfolio_value,
                    execute_trade_fn=execute_trade_fn,
                    update_approval_status=lambda approval_id, status, resolved_by: update_approval_status(
                        supabase_client, approval_id, status, resolved_by
                    ),
                    update_candidate_trade_status=update_candidate_trade_status,
                    log_audit=log_audit,
                )
                print(f"-> {outcome}\n")

        except KeyboardInterrupt:
            print("\nApproval watcher stopped.")
            break
        except Exception as exc:  # noqa: BLE001 -- keep the 24/7 loop alive through transient errors
            print(f"Error while polling approval queue (will retry): {exc}")

        time.sleep(poll_interval_seconds)


if __name__ == "__main__":
    from alpaca.trading.client import TradingClient

    from src.config import load_settings
    from src.db import get_client

    settings = load_settings()
    supabase_client = get_client(settings)
    alpaca_trading_client = TradingClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, paper=not settings.is_live_mode
    )
    poll_approval_queue(supabase_client, alpaca_trading_client)
