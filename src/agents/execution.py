"""
Execution Agent -- STUB, not implemented in Phase 0.

This is the ONLY module that will ever be allowed to call the Alpaca
trading toolset to place an order. Every call path here must, in order:

  1. Load fresh SafetyState from Supabase (never cached).
  2. Call risk.safety_rails.evaluate_trade() -- if not allowed, write an
     audit_log row explaining why and stop. No exceptions, no bypass flag.
  3. Confirm TRADING_MODE == "paper" -- refuse to run at all if "live" mode
     is requested without the explicit manual confirmation step planned for
     the live-trading phase (build plan section 2). That confirmation step
     does not exist yet, so live mode is unreachable by construction today.
  4. Place the order via the Alpaca MCP trading toolset.
  5. Write audit_log + executed_trades rows.

Not implemented yet -- Phase 0 is scaffolding + safety rails only.
"""

from __future__ import annotations


def execute_trade(candidate_trade: dict) -> None:
    raise NotImplementedError("agents.execution: build after risk scorer + safety rails are wired to real data")
