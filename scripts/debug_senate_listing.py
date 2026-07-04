"""
One-off diagnostic for wiring Senate PTR discovery into the per-cycle
universe (the House side is already wired -- see
congressional.fetch_recent_house_ptr_transactions).

WHY THIS EXISTS: congressional.fetch_senate_ptr_listing() talks to
efdsearch.senate.gov's DataTables JSON search endpoint via a multi-step
session/CSRF flow, but its own docstring already flags that it has never
been validated against the live site -- only the PDF-fetch-and-parse path
was validated, using text sourced from an alternate public PDF mirror
(static.notus.org), not this listing endpoint itself. The real shape of
each row this endpoint returns (field names/order, whether it's a dict or
a plain list, how the PDF link is embedded) is genuinely unknown right
now. Guessing at that shape and shipping it into the scheduled, unattended
pipeline would violate this project's own "skip and flag, never guess"
discipline -- so Senate discovery is deliberately NOT wired into
run_cycle() yet.

Run this by hand once (from the project root, with SEC_EDGAR_USER_AGENT
set the same way main.py needs it):

    python scripts/debug_senate_listing.py

Paste the full output back so the real row shape can inform a proper
`_senate_listing_row_to_filing_descriptor()` mapping function, the same
way real evidence (not guesswork) has driven every other fix in this
project.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")  # allow `python scripts/debug_senate_listing.py` from repo root

# Load .env explicitly -- this script doesn't import src.config (which is
# what normally triggers this as a side effect), so without this line
# SEC_EDGAR_USER_AGENT would only be seen if it happened to already be
# exported in the shell, not just present in .env. Same fix as the
# ModuleNotFoundError/missing-sys.path bug found earlier in this same
# script -- missed the pattern every other debug_*.py script already uses.
from dotenv import load_dotenv

load_dotenv()

from src.signals.congressional import fetch_senate_ptr_listing


def main() -> None:
    user_agent = os.getenv("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        raise RuntimeError("SEC_EDGAR_USER_AGENT must be set -- see .env.example")

    today = date.today()
    start = today - timedelta(days=14)

    print(f"Querying efdsearch.senate.gov for PTR filings {start:%m/%d/%Y} - {today:%m/%d/%Y}...")
    rows = fetch_senate_ptr_listing(
        user_agent, start_date=start.strftime("%m/%d/%Y"), end_date=today.strftime("%m/%d/%Y")
    )

    print(f"\nGot {len(rows)} row(s). Raw shape of the first few:\n")
    for i, row in enumerate(rows[:5]):
        print(f"--- row {i} ---")
        print(f"type: {type(row)}")
        print(f"repr: {row!r}")
        print()

    if not rows:
        print(
            "No rows returned for this window -- try widening the date range in this script, "
            "or a filer known to have filed recently, before concluding the endpoint itself is broken."
        )


if __name__ == "__main__":
    main()
