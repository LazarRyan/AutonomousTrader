"""
Congressional trade disclosure signal -- STUB, not implemented in Phase 0.

Planned (build plan section 4): House Clerk + Senate eFD sites directly.
These are PDFs, not clean structured data -- the parser must SKIP AND FLAG
anything it can't parse with confidence rather than guess at a filing's
content. Expect occasional maintenance if site formats change; this is not
fire-and-forget like EDGAR.
"""

from __future__ import annotations


class UnparseableFilingError(Exception):
    """Raised (and the filing skipped+flagged) rather than guessing content."""


def fetch_recent_congressional_trades(symbol: str) -> list[dict]:
    raise NotImplementedError("signals.congressional: build in the signal-generation phase")
