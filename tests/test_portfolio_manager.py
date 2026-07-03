"""
Three tiers, matching src/agents/portfolio_manager.py's structure:

  1. compute_blended_signal_score -- pure arithmetic, always run.
  2. build_portfolio_manager_prompt / parse_portfolio_manager_response --
     pure prompt/parsing logic, always run, no model call.
  3. TestFixtureScenarioProposals -- LLM-in-the-loop, skipped unless
     ANTHROPIC_API_KEY is set. Run by hand once a real key is configured.
"""

import os

import pytest

from src.agents.portfolio_manager import (
    BlendConfig,
    CandidateTradeProposal,
    HoldingSnapshot,
    PortfolioContext,
    SignalSnapshot,
    build_portfolio_manager_prompt,
    compute_blended_signal_score,
    parse_portfolio_manager_response,
)


class TestComputeBlendedSignalScore:
    def test_averages_all_four_sources_equally_by_default(self):
        signal = SignalSnapshot(
            symbol="AAPL",
            momentum_score=100.0,
            insider_score=100.0,
            congressional_score=100.0,
            news_sentiment_score=100.0,
        )
        assert compute_blended_signal_score(signal) == pytest.approx(100.0)

    def test_mixed_signs_average_out(self):
        signal = SignalSnapshot(
            symbol="AAPL",
            momentum_score=100.0,
            insider_score=-100.0,
            congressional_score=100.0,
            news_sentiment_score=-100.0,
        )
        assert compute_blended_signal_score(signal) == pytest.approx(0.0)

    def test_missing_source_renormalizes_remaining_weights(self):
        # Only momentum and insider available, each weighted 0.25 by default
        # -> should renormalize to 0.5/0.5, not silently divide the missing
        # source's weight by treating it as zero.
        signal = SignalSnapshot(symbol="AAPL", momentum_score=100.0, insider_score=50.0)
        result = compute_blended_signal_score(signal)
        assert result == pytest.approx(75.0)  # (100*0.5 + 50*0.5)

    def test_single_available_source_equals_its_own_score(self):
        signal = SignalSnapshot(symbol="AAPL", momentum_score=42.0)
        assert compute_blended_signal_score(signal) == pytest.approx(42.0)

    def test_all_sources_none_raises(self):
        signal = SignalSnapshot(symbol="AAPL")
        with pytest.raises(ValueError):
            compute_blended_signal_score(signal)

    def test_custom_weights_respected(self):
        config = BlendConfig(
            momentum_weight=0.7, insider_weight=0.1, congressional_weight=0.1, news_sentiment_weight=0.1
        )
        signal = SignalSnapshot(
            symbol="AAPL",
            momentum_score=100.0,
            insider_score=0.0,
            congressional_score=0.0,
            news_sentiment_score=0.0,
        )
        assert compute_blended_signal_score(signal, config) == pytest.approx(70.0)

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError):
            BlendConfig(momentum_weight=0.5, insider_weight=0.5, congressional_weight=0.5, news_sentiment_weight=0.5)


class TestBuildPortfolioManagerPrompt:
    def _portfolio(self, **overrides) -> PortfolioContext:
        defaults = dict(total_portfolio_value=100_000.0, cash_available=40_000.0, holdings=[])
        defaults.update(overrides)
        return PortfolioContext(**defaults)

    def test_includes_scores_and_portfolio_context(self):
        prompt = build_portfolio_manager_prompt({"AAPL": 75.5}, self._portfolio())
        assert "AAPL" in prompt
        assert "75.5" in prompt
        assert "100,000.00" in prompt
        assert "40,000.00" in prompt

    def test_includes_holdings_when_present(self):
        portfolio = self._portfolio(
            holdings=[HoldingSnapshot(symbol="MSFT", quantity=10, avg_entry_price=300.0)]
        )
        prompt = build_portfolio_manager_prompt({"AAPL": 10.0}, portfolio)
        assert "MSFT" in prompt
        assert "300.00" in prompt

    def test_notes_no_holdings_when_empty(self):
        prompt = build_portfolio_manager_prompt({"AAPL": 10.0}, self._portfolio())
        assert "no current holdings" in prompt

    def test_raises_on_empty_scores(self):
        with pytest.raises(ValueError):
            build_portfolio_manager_prompt({}, self._portfolio())


class TestParsePortfolioManagerResponse:
    def test_parses_clean_array(self):
        response = '[{"symbol": "aapl", "side": "buy", "quantity": 10, "reasoning": "Strong momentum."}]'
        proposals = parse_portfolio_manager_response(response)
        assert len(proposals) == 1
        p = proposals[0]
        assert isinstance(p, CandidateTradeProposal)
        assert p.symbol == "AAPL"  # normalized to uppercase
        assert p.side == "buy"
        assert p.quantity == 10.0

    def test_empty_array_is_valid_no_trades(self):
        assert parse_portfolio_manager_response("[]") == []

    def test_strips_markdown_code_fence(self):
        response = '```json\n[{"symbol": "MSFT", "side": "sell", "quantity": 5, "reasoning": "Trim position."}]\n```'
        proposals = parse_portfolio_manager_response(response)
        assert proposals[0].symbol == "MSFT"

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response("not json")

    def test_raises_on_non_array_root(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response('{"symbol": "AAPL"}')

    def test_raises_on_invalid_side(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response(
                '[{"symbol": "AAPL", "side": "hold", "quantity": 1, "reasoning": "x"}]'
            )

    def test_raises_on_nonpositive_quantity(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response(
                '[{"symbol": "AAPL", "side": "buy", "quantity": 0, "reasoning": "x"}]'
            )

    def test_raises_on_missing_reasoning(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response('[{"symbol": "AAPL", "side": "buy", "quantity": 1}]')

    def test_raises_on_missing_symbol(self):
        with pytest.raises(ValueError):
            parse_portfolio_manager_response('[{"side": "buy", "quantity": 1, "reasoning": "x"}]')

    def test_parses_multiple_proposals(self):
        response = (
            '[{"symbol": "AAPL", "side": "buy", "quantity": 10, "reasoning": "a"},'
            '{"symbol": "MSFT", "side": "sell", "quantity": 5, "reasoning": "b"}]'
        )
        proposals = parse_portfolio_manager_response(response)
        assert len(proposals) == 2

    def test_tolerates_invalid_backslash_escaped_apostrophe(self):
        # Same real bug class as news_sentiment.py's identical regression
        # test: a JS/Python-style escaped apostrophe inside a JSON string
        # (e.g. "AWS\'s big bet") is invalid JSON and previously caused a
        # hard parse failure on an otherwise well-formed response.
        response = r'[{"symbol": "AMZN", "side": "buy", "quantity": 10, "reasoning": "AWS\'s big bet is bullish."}]'
        proposals = parse_portfolio_manager_response(response)
        assert len(proposals) == 1
        assert proposals[0].reasoning == "AWS's big bet is bullish."


# ============================================================
# LLM-in-the-loop fixture test. Skipped unless ANTHROPIC_API_KEY is set.
# ============================================================


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="requires a real ANTHROPIC_API_KEY -- run by hand to sanity-check the prompt",
)
class TestFixtureScenarioProposals:
    """Sends clear-cut scenarios to the real model and checks direction,
    not exact values -- LLM output isn't bit-for-bit deterministic.
    """

    def _propose(self, blended_scores: dict, portfolio: PortfolioContext) -> list[CandidateTradeProposal]:
        from src.agents.portfolio_manager import propose_candidate_trades

        return propose_candidate_trades(
            blended_scores, portfolio, anthropic_api_key=os.environ["ANTHROPIC_API_KEY"]
        )

    def test_strongly_bullish_signal_with_cash_and_no_position_proposes_a_buy(self):
        portfolio = PortfolioContext(total_portfolio_value=100_000.0, cash_available=50_000.0, holdings=[])
        proposals = self._propose({"ACME": 95.0}, portfolio)
        assert any(p.symbol == "ACME" and p.side == "buy" for p in proposals), proposals

    def test_strongly_bearish_signal_on_held_position_proposes_a_sell(self):
        portfolio = PortfolioContext(
            total_portfolio_value=100_000.0,
            cash_available=20_000.0,
            holdings=[HoldingSnapshot(symbol="ACME", quantity=100, avg_entry_price=50.0)],
        )
        proposals = self._propose({"ACME": -95.0}, portfolio)
        assert any(p.symbol == "ACME" and p.side == "sell" for p in proposals), proposals

    def test_near_neutral_signal_with_no_position_proposes_nothing_for_it(self):
        portfolio = PortfolioContext(total_portfolio_value=100_000.0, cash_available=50_000.0, holdings=[])
        proposals = self._propose({"ACME": 3.0}, portfolio)
        assert not any(p.symbol == "ACME" for p in proposals), proposals
