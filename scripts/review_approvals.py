"""
CLI approval watcher -- STUB, not implemented in Phase 0.

Planned (build plan section 4b): poll the `approval_queue` table in
Supabase. The moment a high-risk trade lands, print the trade + full
reasoning (signals, risk score, why it crossed the threshold) and block on
a y/n right there in the terminal. Nothing executes until answered. Email
notification fires in parallel as a backup/heads-up only -- this CLI is the
actual approval mechanism, no separate web UI in v1.

Run with: python scripts/review_approvals.py
"""

from __future__ import annotations

import time


def poll_approval_queue(poll_interval_seconds: int = 30) -> None:
    raise NotImplementedError(
        "scripts.review_approvals: build after approval_queue is populated by a real risk scorer run"
    )


if __name__ == "__main__":
    poll_approval_queue()
