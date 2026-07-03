"""
Two tiers of tests, matching the two-tier design of src/signals/news_sentiment.py:

  1. Pure-logic tests (always run, no credentials): prompt construction and
     response parsing/validation, using canned strings -- no network, no
     model call.

  2. LLM-in-the-loop fixture test (TestFixtureHeadlineDirection): sends real
     fixture headlines to the real Anthropic API and checks the score comes
     back with the expected sign. Skipped automatically unless
     ANTHROPIC_API_KEY is set in the environment -- run this by hand once a
     real key is configured, as a sanity check that the prompt still
     elicits correctly-signed scores. Not part of the default test run.
"""

import os

import pytest

from src.signals.news_sentiment import (
    ParsedSentiment,
    build_sentiment_prompt,
    parse_sentiment_response,
)


class TestBuildSentimentPrompt:
    def test_includes_symbol_and_numbered_headlines(self):
        prompt = build_sentiment_prompt("AAPL", ["Headline one", "Headline two"])
        assert "AAPL" in prompt
        assert "1. Headline one" in prompt
        assert "2. Headline two" in prompt

    def test_raises_on_empty_headlines(self):
        with pytest.raises(ValueError):
            build_sentiment_prompt("AAPL", [])


class TestParseSentimentResponse:
    def test_parses_clean_json(self):
        result = parse_sentiment_response('{"score": 65, "reasoning": "Strong earnings beat."}')
        assert isinstance(result, ParsedSentiment)
        assert result.score == 65.0
        assert result.reasoning == "Strong earnings beat."

    def test_parses_negative_score(self):
        result = parse_sentiment_response('{"score": -80, "reasoning": "Profit warning issued."}')
        assert result.score == -80.0

    def test_strips_markdown_code_fence(self):
        response = '```json\n{"score": 10, "reasoning": "Mildly positive."}\n```'
        result = parse_sentiment_response(response)
        assert result.score == 10.0

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError):
            parse_sentiment_response("this is not json")

    def test_raises_on_missing_score_field(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"reasoning": "no score field"}')

    def test_raises_on_missing_reasoning_field(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": 10}')

    def test_raises_on_out_of_range_score_high(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": 150, "reasoning": "too high"}')

    def test_raises_on_out_of_range_score_low(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": -150, "reasoning": "too low"}')

    def test_raises_on_non_numeric_score(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": "very bullish", "reasoning": "not a number"}')

    def test_raises_on_empty_reasoning(self):
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": 10, "reasoning": ""}')

    def test_accepts_boundary_scores(self):
        assert parse_sentiment_response('{"score": 100, "reasoning": "max bullish"}').score == 100.0
        assert parse_sentiment_response('{"score": -100, "reasoning": "max bearish"}').score == -100.0

    def test_tolerates_invalid_backslash_escaped_apostrophe(self):
        # Regression test for a real bug: a live Anthropic response scoring
        # AMZN news contained a JS/Python-style escaped apostrophe
        # ("AWS\'s $1B AI engineering bet...") inside the reasoning string,
        # which is invalid JSON (backslash may only precede " \ / b f n r t
        # u) and previously caused a hard parse failure on an otherwise
        # well-formed, correctly-scored response.
        response = r'{"score": 45, "reasoning": "AWS\'s $1B AI engineering bet is bullish."}'
        result = parse_sentiment_response(response)
        assert result.score == 45.0
        assert result.reasoning == "AWS's $1B AI engineering bet is bullish."

    def test_still_rejects_genuinely_invalid_json(self):
        # The escape-repair pass shouldn't turn a truly broken response
        # into a silently-accepted one.
        with pytest.raises(ValueError):
            parse_sentiment_response('{"score": 45, "reasoning": "unterminated string')

    def test_truncated_response_error_mentions_truncation(self):
        # Regression test for a real bug: two separate live responses
        # (AMZN, NVDA) were cut off mid-JSON at exactly max_tokens, always
        # right after the reasoning string closed but before the final
        # "}". The error message should say so rather than just "not valid
        # JSON", since the fix (raise max_tokens) is different from a
        # genuine formatting bug.
        truncated = '{"score": 20, "reasoning": "Some real analysis text that never got a closing brace."'
        with pytest.raises(ValueError, match="truncated"):
            parse_sentiment_response(truncated)


# ============================================================
# LLM-in-the-loop fixture test. Skipped unless ANTHROPIC_API_KEY is set.
# ============================================================

FIXTURE_HEADLINES = {
    "bullish": [
        "Acme Corp beats Q3 earnings estimates by 30%, raises full-year guidance",
        "Acme Corp announces $2B share buyback program after record quarterly revenue",
        "Analysts upgrade Acme Corp to 'Buy' citing accelerating cloud growth",
    ],
    "bearish": [
        "Acme Corp issues profit warning, cuts full-year guidance for second time this year",
        "Acme Corp CFO resigns amid accounting practices investigation",
        "Acme Corp shares halted after reports of major product safety recall",
    ],
    "neutral": [
        "Acme Corp to present at industry conference next month",
        "Acme Corp names new head of investor relations",
        "Acme Corp's annual shareholder meeting scheduled for next Tuesday",
    ],
}


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"), reason="requires a real ANTHROPIC_API_KEY -- run by hand to sanity-check the prompt"
)
class TestFixtureHeadlineDirection:
    """Sends real headlines to the real model. Loose thresholds (not exact
    values) because LLM output isn't bit-for-bit deterministic -- this is a
    sanity check on DIRECTION and rough magnitude, not a precise regression
    test.
    """

    def _score(self, headlines: list[str]) -> float:
        from src.signals.news_sentiment import score_news_sentiment

        result = score_news_sentiment("ACME", headlines, anthropic_api_key=os.environ["ANTHROPIC_API_KEY"])
        return result.score

    def test_bullish_headlines_score_positive(self):
        score = self._score(FIXTURE_HEADLINES["bullish"])
        assert score > 30, f"expected strongly bullish score, got {score}"

    def test_bearish_headlines_score_negative(self):
        score = self._score(FIXTURE_HEADLINES["bearish"])
        assert score < -30, f"expected strongly bearish score, got {score}"

    def test_neutral_headlines_score_near_zero(self):
        score = self._score(FIXTURE_HEADLINES["neutral"])
        assert -25 <= score <= 25, f"expected roughly neutral score, got {score}"
