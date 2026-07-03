"""
News sentiment signal -- STUB, not implemented in Phase 0.

Planned (build plan section 4): Alpaca News API (get_news -- already
available in the market-data toolset, no new permission needed) -> an LLM
agent scores sentiment/relevance per symbol in the trading universe. This
is the one signal source that is intentionally LLM-based rather than
deterministic.
"""

from __future__ import annotations


def score_news_sentiment(symbol: str) -> float:
    raise NotImplementedError("signals.news_sentiment: build in the signal-generation phase")
