"""
Insider trade (Form 4) signal via SEC EDGAR -- STUB, not implemented in Phase 0.

Planned (build plan section 4): pull directly from data.sec.gov
(submissions / full-text-search APIs), free and authoritative -- no auth,
no third-party reseller. Deterministic filing -> signal mapping, unit-tested
against fixture EDGAR responses.
"""

from __future__ import annotations


def fetch_recent_insider_trades(symbol: str) -> list[dict]:
    raise NotImplementedError("signals.insider_edgar: build in the signal-generation phase")
