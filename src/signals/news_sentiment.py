"""
News sentiment signal -- the one intentionally LLM-based signal source
(build plan section 4: "Alpaca News API -> an LLM agent scores
sentiment/relevance per symbol").

This module is split differently than the deterministic signals
(momentum.py, insider_edgar.py, congressional.py), because "is this
headline bullish or bearish for this symbol" isn't a pure function you can
fully specify with arithmetic -- it genuinely needs a model's judgment. The
split here is:

  1. PURE, unit-tested logic with NO network/model calls:
     build_sentiment_prompt() (deterministic prompt construction) and
     parse_sentiment_response() (deterministic parsing/validation of the
     model's JSON reply). Covered by tests/test_news_sentiment.py using
     canned response strings -- these tests always run, no API key needed.

  2. An LLM-in-the-loop integration test: tests/test_news_sentiment.py also
     contains a small set of fixture headlines with an expected sentiment
     DIRECTION (clearly bullish / clearly bearish / neutral) that are
     actually sent to the real Anthropic API and checked against loose
     thresholds. This test is skipped automatically if ANTHROPIC_API_KEY
     isn't set -- it's meant to be run by hand once a real key is in .env,
     as a sanity check that the prompt still elicits sensible, correctly-
     signed scores. It is NOT part of the default "no credentials needed"
     test run.

  3. Thin network wrappers (fetch_recent_news, score_news_sentiment): talk
     to the Alpaca News API and the Anthropic API. Not unit-tested
     themselves -- they're thin glue around the tested pieces above.

Score convention: -100 (maximally bearish) to +100 (maximally bullish), 0 =
neutral/irrelevant. The model is instructed to weigh RELEVANCE as well as
direction -- a glowing headline about an unrelated product line should
score close to 0, not +100, if it doesn't bear on the symbol's outlook.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# JSON only permits a backslash to be followed by one of: " \ / b f n r t u
# (the start of a \\uXXXX escape). A backslash before any other character is
# invalid JSON and fails to parse -- observed in practice: a real Anthropic
# response scoring AMZN news used a JS/Python-style escaped apostrophe
# ("AWS\'s $1B AI engineering bet...") inside the "reasoning" string, which
# is invalid JSON even though the overall shape was otherwise correct. Since
# this is essentially always a cosmetic over-escaping mistake rather than a
# sign the response is genuinely malformed, the fix is to drop the stray
# backslash and keep the character, not to fail the whole response over it.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _fix_invalid_backslash_escapes(text: str) -> str:
    return _INVALID_JSON_ESCAPE_RE.sub("", text)


_SYSTEM_PROMPT = """You are a financial news sentiment scorer for an automated trading signal.

For the given stock symbol and list of recent headlines, produce a single composite sentiment
score reflecting how bullish or bearish the news is for that symbol's near-term stock price.

Scoring rules:
- Score from -100 (maximally bearish) to +100 (maximally bullish). 0 = neutral or irrelevant.
- Weigh RELEVANCE as well as direction. A very positive headline about something that doesn't
  bear on the company's business or outlook should score close to 0, not near +100.
- Consider the whole set of headlines together, not just the single strongest one.
- If headlines conflict, weigh them and produce a net score rather than picking one side.

Respond with ONLY a single JSON object, no other text before or after it, in exactly this shape.
Put all explanation inside the "reasoning" field -- do not add any commentary outside the JSON object
itself:
{"score": <number between -100 and 100>, "reasoning": "<one or two sentence explanation>"}
"""


def build_sentiment_prompt(symbol: str, headlines: list[str]) -> str:
    """Deterministic user-message construction. Pure function -- fully
    unit-tested, no network/model call."""
    if not headlines:
        raise ValueError("build_sentiment_prompt requires at least one headline")

    numbered = "\n".join(f"{i + 1}. {headline}" for i, headline in enumerate(headlines))
    return f"Symbol: {symbol}\n\nRecent headlines:\n{numbered}"


@dataclass(frozen=True)
class ParsedSentiment:
    score: float
    reasoning: str


def parse_sentiment_response(response_text: str) -> ParsedSentiment:
    """Parse and validate the model's JSON reply. Raises ValueError on
    anything that doesn't match the expected shape -- a malformed response
    means the prompt or the model's behavior has drifted and needs
    attention, not a silent fallback to some default score.
    """
    text = response_text.strip()

    # Models occasionally wrap JSON in a fenced code block despite
    # instructions not to -- strip that defensively before parsing.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    # Drop any invalid backslash escapes (see _fix_invalid_backslash_escapes)
    # before parsing -- a real, otherwise-well-formed response has been seen
    # to contain one.
    text = _fix_invalid_backslash_escapes(text)

    # Use raw_decode (rather than json.loads) so a model that appends stray
    # commentary after the JSON object despite instructions not to doesn't
    # cause a hard parse failure -- we only need the first complete JSON
    # value in the string, and ignore anything after it.
    try:
        payload, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        # A parse error whose position lands exactly at the end of the
        # string (rather than somewhere in the middle) is the signature of
        # a truncated response -- seen twice in practice (AMZN, NVDA) with
        # max_tokens set too low. Surface that distinction rather than
        # leaving it to be rediscovered by hand each time.
        hint = ""
        if exc.pos >= len(text):
            hint = " (response appears truncated -- got cut off before valid JSON completed; consider raising max_tokens)"
        raise ValueError(f"Sentiment response was not valid JSON{hint}: {response_text!r}") from exc

    if not isinstance(payload, dict) or "score" not in payload or "reasoning" not in payload:
        raise ValueError(f"Sentiment response missing required fields: {response_text!r}")

    score = payload["score"]
    reasoning = payload["reasoning"]

    if not isinstance(score, (int, float)):
        raise ValueError(f"Sentiment response 'score' is not numeric: {score!r}")
    if not (-100 <= score <= 100):
        raise ValueError(f"Sentiment response 'score' out of range [-100, 100]: {score!r}")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError(f"Sentiment response 'reasoning' is missing or empty: {reasoning!r}")

    return ParsedSentiment(score=float(score), reasoning=reasoning)


@dataclass(frozen=True)
class NewsSentimentResult:
    symbol: str
    score: float
    reasoning: str
    num_headlines_considered: int


# ============================================================
# Network / LLM wrappers -- not unit-tested here (require live network and,
# for score_news_sentiment, a real Anthropic API call). See
# tests/test_news_sentiment.py for the skip-if-no-key integration check.
# ============================================================


def fetch_recent_news(symbol: str, api_key: str, secret_key: str, limit: int = 20) -> list[str]:
    """Fetch recent headlines for a symbol from Alpaca's News API (already
    available in the market-data toolset -- no new Alpaca permission
    needed for this signal, unlike the trading toolset used by execution).
    """
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(api_key, secret_key)
    request = NewsRequest(symbols=symbol, limit=limit)
    response = client.get_news(request)

    # alpaca-py's NewsSet stores articles at .data["news"], not a top-level
    # .news attribute (confirmed against alpaca-py's actual NewsSet model --
    # response.news raised AttributeError against a real account during the
    # first live dry run).
    return [article.headline for article in response.data["news"]]


def _extract_response_text(response) -> str:
    """Anthropic responses aren't guaranteed to have the text reply in
    content[0] -- e.g. an extended-thinking block can precede it (observed
    in practice with claude-sonnet-5, even without thinking explicitly
    requested here). Find the first block with type == "text" instead of
    assuming position."""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError(f"No text block found in Anthropic response: {response.content!r}")


def score_news_sentiment(
    symbol: str, headlines: list[str], anthropic_api_key: str, model: str = "claude-sonnet-5"
) -> NewsSentimentResult:
    """End-to-end: build the prompt, call the Anthropic API, parse the
    reply. Thin glue around the tested pure functions above.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    user_prompt = build_sentiment_prompt(symbol, headlines)

    # max_tokens=300 was too low in practice: two separate real responses
    # (AMZN, NVDA) were cut off mid-JSON, always right after the closing
    # quote of "reasoning" but before the final "}" -- a genuine truncation,
    # not an escaping issue (that's handled separately by
    # _fix_invalid_backslash_escapes). This model can also emit a
    # ThinkingBlock ahead of the visible text (see _extract_response_text)
    # whose tokens count against the same max_tokens budget, leaving less
    # room than expected for the actual JSON reply. Raised with real margin.
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    response_text = _extract_response_text(response)
    parsed = parse_sentiment_response(response_text)

    return NewsSentimentResult(
        symbol=symbol,
        score=parsed.score,
        reasoning=parsed.reasoning,
        num_headlines_considered=len(headlines),
    )
