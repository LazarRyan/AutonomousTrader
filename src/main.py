"""
Entry point -- STUB, not implemented in Phase 0.

Planned run loop (scheduled every 15-30 min during market hours):
  1. Load config + safety_state from Supabase.
  2. If kill switch engaged or daily/weekly halted -> log and exit early.
  3. Generate signals (momentum, insider, congressional, news sentiment).
  4. Portfolio Manager Agent proposes candidate trades.
  5. Risk scorer scores each candidate.
  6. Below threshold -> Execution Agent (paper only). At/above -> approval_queue.
  7. Everything, taken or not, gets an audit_log row.

Phase 0 deliberately stops before any of this exists -- see
src/risk/scorer.py and src/risk/safety_rails.py for what's actually built
and tested so far.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "main: Phase 0 is scaffolding only. Signal generation, the portfolio "
        "manager agent, and the execution agent are not built yet."
    )


if __name__ == "__main__":
    main()
