"""
Deterministic momentum signal -- STUB, not implemented in Phase 0.

Planned (build plan section 4): 20/50-day moving average crossover +
RSI(14) + 10-day rate of change, on Alpaca historical bars, combined into
one momentum score per symbol. No LLM. Must be unit-tested with fixture
bar data before it feeds the Portfolio Manager Agent.
"""

from __future__ import annotations


def compute_momentum_score(symbol: str) -> float:
    raise NotImplementedError("signals.momentum: build in the signal-generation phase")
