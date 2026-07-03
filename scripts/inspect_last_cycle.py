"""
Quick diagnostic: prints the most recent audit_log, candidate_trades, and
approval_queue rows directly from your terminal, using the same Supabase
credentials already in .env -- no need to open the Supabase web dashboard
just to check what the last dry run / cycle actually did.

Run with: python scripts/inspect_last_cycle.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from src.config import load_settings
from src.db import get_client


def _print_rows(title: str, rows: list[dict]) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70)
    if not rows:
        print("(no rows)")
    for row in rows:
        print(row)
    print()


def main() -> None:
    settings = load_settings()
    client = get_client(settings)

    audit_rows = (
        client.table("audit_log").select("*").order("created_at", desc=True).limit(10).execute().data
    )
    _print_rows("Most recent audit_log rows", audit_rows)

    candidate_rows = (
        client.table("candidate_trades").select("*").order("created_at", desc=True).limit(10).execute().data
    )
    _print_rows("Most recent candidate_trades rows", candidate_rows)

    approval_rows = (
        client.table("approval_queue").select("*").eq("status", "pending").execute().data
    )
    _print_rows("Pending approval_queue rows", approval_rows)


if __name__ == "__main__":
    main()
