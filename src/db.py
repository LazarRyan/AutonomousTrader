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


def write_signal(
    client: Client,
    symbol: str,
    signal_type: str,
    score: float | None,
    raw_data: dict[str, Any] | None = None,
) -> None:
    """Append one row to the `signals` table: one per source (momentum,
    insider, congressional, news_sentiment) per symbol per cycle.

    This is the per-source detail that compute_blended_signal_score's input
    (SignalSnapshot) doesn't retain -- the blended score alone can't tell you
    *why* a symbol scored the way it did, only candidate_trades.blended_signal_score
    plus this table together can. `raw_data` should carry the full result
    dataclass (via dataclasses.asdict) so the source's own `reasoning` string
    and component breakdown (e.g. momentum's SMA/RSI/ROC components, insider's
    buy/sell counts) are all queryable, not just the final numeric score.

    Deliberately best-effort, unlike write_audit_log: this is observability/
    debugging detail layered on top of the decision trail, not the decision
    trail itself (audit_log / candidate_trades / approval_queue already
    capture every actual decision and its reasoning independently of this
    table). A write failure here is logged and swallowed so one Supabase
    hiccup on telemetry can't take down signal gathering for a symbol.
    """
    try:
        client.table("signals").insert(
            {
                "symbol": symbol,
                "signal_type": signal_type,
                "score": score,
                "raw_data": raw_data or {},
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 -- best-effort telemetry, see docstring
        print(f"[{symbol}] failed to write '{signal_type}' signal detail to signals table: {exc}")


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
