"""
Tests for the memory/market-context additions to the portfolio manager's
pure pieces: BlendConfig.from_weights (adaptive weights loading) and the
optional memory_context/market_context sections of
build_portfolio_manager_prompt. The original prompt/parse behavior is
covered in test_portfolio_manager.py -- this file only covers what the
learning upgrade added.
"""

import pytest

from src.agents.portfolio_manager import (
    BlendConfig,
    PortfolioContext,
    build_portfolio_manager_prompt,
)


class TestBlendConfigFromWeights:
    def test_none_and_empty_fall_back_to_equal_defaults(self):
        assert BlendConfig.from_weights(None) == BlendConfig()
        assert BlendConfig.from_weights({}) == BlendConfig()

    def test_valid_weights_applied(self):
        config = BlendConfig.from_weights(
            {"momentum": 0.4, "insider": 0.1, "congressional": 0.1, "news_sentiment": 0.4}
        )
        assert config.momentum_weight == 0.4
        assert config.insider_weight == 0.1

    def test_wrong_keys_raise(self):
        with pytest.raises(ValueError, match="keys"):
            BlendConfig.from_weights({"momentum": 0.5, "vibes": 0.5})

    def test_bad_sum_raises_via_post_init(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            BlendConfig.from_weights({"momentum": 0.5, "insider": 0.5, "congressional": 0.5, "news_sentiment": 0.5})


class TestPromptContextSections:
    def _portfolio(self) -> PortfolioContext:
        return PortfolioContext(total_portfolio_value=100_000.0, cash_available=5_000.0, holdings=[])

    def test_no_contexts_is_the_original_prompt(self):
        prompt = build_portfolio_manager_prompt({"KHC": 38.4}, self._portfolio())
        assert "Your memory" not in prompt
        assert "Market regime" not in prompt

    def test_memory_context_appended_verbatim(self):
        memory = "Your memory (from your own past cycles -- use it, cite it in your reasoning):\n  KHC: bought yesterday"
        prompt = build_portfolio_manager_prompt({"KHC": 38.4}, self._portfolio(), memory_context=memory)
        assert prompt.endswith("  KHC: bought yesterday")
        assert prompt.index("Blended signal scores") < prompt.index("Your memory")

    def test_market_context_before_memory_context(self):
        prompt = build_portfolio_manager_prompt(
            {"KHC": 38.4}, self._portfolio(), memory_context="MEMORY_BLOCK", market_context="MARKET_BLOCK"
        )
        assert prompt.index("MARKET_BLOCK") < prompt.index("MEMORY_BLOCK")

    def test_empty_string_contexts_omitted(self):
        prompt = build_portfolio_manager_prompt({"KHC": 38.4}, self._portfolio(), memory_context="", market_context="")
        assert prompt == build_portfolio_manager_prompt({"KHC": 38.4}, self._portfolio())
