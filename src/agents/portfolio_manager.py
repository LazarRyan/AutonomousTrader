"""
Portfolio Manager Agent -- STUB, not implemented in Phase 0.

Planned: an LLM agent that reads blended signals (momentum + insider +
congressional + news sentiment) plus current holdings, and proposes
candidate trades (symbol, side, size) with reasoning. It NEVER decides
whether a trade executes -- that's risk/scorer.py + risk/safety_rails.py,
both deterministic and downstream of this. This agent only proposes.
"""

from __future__ import annotations


def propose_candidate_trades() -> list[dict]:
    raise NotImplementedError("agents.portfolio_manager: build after signal sources exist")
