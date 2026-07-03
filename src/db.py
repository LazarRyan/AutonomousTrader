"""
Thin Supabase client wrapper.

Every table write in this project should go through here so that the audit
log discipline (section 6 of the build plan: "every decision, taken or not,
and why -- persisted, never silently dropped") is structurally hard to skip.
Phase 0: connection + typed helpers for the tables that exist so far
(safety_state, config, audit_log). Signal/trade tables get helpers added
alongside the signal and execution modules in later phases.
"""

from __future__ import annotations

from typing import Any

from supabase import Client, create_client

from src.config import Settings


def get_client(settings: Settings) -> Client:
    return create_client(settings.supabase_url, settings.supabase_key)


def write_audit_log(
    client: Client,
    event_type: str,
    decision: str,
    reasoning: str,
    symbol: str | None = None,
    candidate_trade_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one row to audit_log. Never raises silently -- if this write
    fails, that's loud-crash territory, not swallow-and-continue territory,
    because a decision with no audit trail is exactly what section 6 forbids.
    """
    client.table("audit_log").insert(
        {
            "event_type": event_type,
            "symbol": symbol,
            "candidate_trade_id": candidate_trade_id,
            "decision": decision,
            "reasoning": reasoning,
            "metadata": metadata or {},
        }
    ).execute()


def get_safety_state(client: Client) -> dict[str, Any]:
    result = client.table("safety_state").select("*").limit(1).execute()
    if not result.data:
        raise RuntimeError("safety_state table has no row -- schema migration incomplete")
    return result.data[0]


def get_config(client: Client) -> dict[str, Any]:
    result = client.table("config").select("*").limit(1).execute()
    if not result.data:
        raise RuntimeError("config table has no row -- schema migration incomplete")
    return result.data[0]
