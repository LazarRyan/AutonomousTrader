"""
Portfolio Manager Agent.

Reads blended signals (momentum + insider + congressional + news sentiment)
plus current holdings, and proposes candidate trades (symbol, side,
quantity) with reasoning. This agent NEVER decides whether a trade
executes -- that's risk/scorer.py (needs approval?) and
risk/safety_rails.py (non-negotiable hard limits), both deterministic and
strictly downstream of this. This agent only proposes; every proposal it
makes still has to clear both of those gates before anything happens.

Split, same pattern as news_sentiment.py:

  1. PURE, unit-tested logic, no model call:
       - compute_blended_signal_score(): combining the four raw signal
         scores (-100..100 each) into one blended score per symbol is
         plain weighted-average arithmetic -- deterministic, so it's done
         here rather than left to the LLM's judgment. Handles missing
         signal sources by renormalizing weights over whatever's available,
         rather than treating a missing signal as a neutral 0 (which would
         quietly understate conviction whenever a source is down).
       - build_portfolio_manager_prompt(): deterministic prompt construction
         from blended scores + portfolio context.
       - parse_portfolio_manager_response(): deterministic parsing/
         validation of the model's JSON reply into CandidateTradeProposal
         objects.
     All covered by tests/test_portfolio_manager.py with canned inputs --
     no network, no API key needed.

  2. An LLM-in-the-loop fixture test (see tests/test_portfolio_manager.py):
     a couple of clear-cut scenarios (strongly bullish signal + no
     position + cash available; strongly bearish signal + existing
     position) sent to the real Anthropic API, checked for the expected
     side/symbol. Skipped automatically unless ANTHROPIC_API_KEY is set --
     run by hand as a sanity check, not part of the default test run.

  3. propose_candidate_trades(): thin wrapper that calls the Anthropic API.
     Not unit-tested itself -- thin glue around the tested pieces above.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# JSON only permits a backslash to be followed by one of: " \ / b f n r t u
# (the start of a \\uXXXX escape). A backslash before any other character is
# invalid JSON and fails to parse -- a real Anthropic response for the news
# sentiment signal (same underlying model/prompting pattern as this module)
# was seen to contain a JS/Python-style escaped apostrophe inside a
# "reasoning" string ("AWS\'s $1B AI engineering bet..."), which is invalid
# JSON even though the overall shape was otherwise correct. Since this is
# essentially always a cosmetic over-escaping mistake rather than a sign the
# response is genuinely malformed, the fix is to drop the stray backslash
# and keep the character, not to fail the whole response over it. Same fix
# as src/signals/news_sentiment.py's _fix_invalid_backslash_escapes.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _fix_invalid_backslash_escapes(text: str) -> str:
    return _INVALID_JSON_ESCAPE_RE.sub("", text)


# ============================================================
# Blended signal scoring -- deterministic, no LLM.
# ============================================================


@dataclass(frozen=True)
class BlendConfig:
    momentum_weight: float = 0.25
    insider_weight: float = 0.25
    congressional_weight: float = 0.25
    news_sentiment_weight: float = 0.25

    def __post_init__(self) -> None:
        total = (
            self.momentum_weight
            + self.insider_weight
            + self.congressional_weight
            + self.news_sentiment_weight
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Blend weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class SignalSnapshot:
    symbol: str
    momentum_score: float | None = None
    insider_score: float | None = None
    congressional_score: float | None = None
    news_sentiment_score: float | None = None


def compute_blended_signal_score(signal: SignalSnapshot, config: BlendConfig | None = None) -> float:
    """Weighted average of whichever signal sources are available for this
    symbol, with weights renormalized over the available subset. Raises
    ValueError if every source is None -- there's nothing to blend, and a
    silent 0.0 would be indistinguishable from a genuinely neutral reading
    across all four sources, which is a materially different situation.
    """
    config = config or BlendConfig()

    components = [
        (signal.momentum_score, config.momentum_weight),
        (signal.insider_score, config.insider_weight),
        (signal.congressional_score, config.congressional_weight),
        (signal.news_sentiment_score, config.news_sentiment_weight),
    ]

    available = [(score, weight) for score, weight in components if score is not None]
    if not available:
        raise ValueError(f"No signal sources available to blend for {signal.symbol}")

    total_weight = sum(weight for _, weight in available)
    return sum(score * weight for score, weight in available) / total_weight


# ============================================================
# Candidate trade proposals -- prompt + response parsing, no LLM call here.
# ============================================================


@dataclass(frozen=True)
class HoldingSnapshot:
    symbol: str
    quantity: float
    avg_entry_price: float


@dataclass(frozen=True)
class PortfolioContext:
    total_portfolio_value: float
    cash_available: float
    holdings: list[HoldingSnapshot]


@dataclass(frozen=True)
class CandidateTradeProposal:
    symbol: str
    side: str  # "buy" or "sell"
    quantity: float
    reasoning: str


_SYSTEM_PROMPT = """You are a portfolio manager agent for an automated paper-trading system.

You are given a blended signal score (-100 to +100, positive = bullish) for each symbol under
consideration, and the current portfolio context (total value, cash available, existing holdings).

Propose zero or more candidate trades. You are NOT responsible for risk approval or position-size
limits -- a separate deterministic system enforces those after you propose. However, use reasonable
judgment: don't propose spending more cash than is available, don't propose selling more shares of a
symbol than are currently held, and don't propose a trade for a symbol with a weak or near-neutral
blended score just to have something to say.

It is completely normal and often correct to propose NO trades in a given cycle. If you don't want to
trade a symbol -- including a symbol with a strongly bearish score that you can't act on because you
hold no shares of it and this account cannot short -- simply leave it out of the array entirely. Never
include a placeholder or zero-quantity entry to explain why you're skipping a symbol; every object in
the array must be a real, executable trade with a positive quantity.

Your ENTIRE reply must be a single JSON array and nothing else. Do not write any explanation,
preamble, or summary before or after it, even a single sentence -- the very first character of your
reply must be "[". Put all explanation inside the "reasoning" field of each proposal (empty array "[]"
if no trades are warranted):
[{"symbol": "<TICKER>", "side": "buy" | "sell", "quantity": <positive number>, "reasoning": "<one or two sentence explanation>"}]
"""


def build_portfolio_manager_prompt(
    blended_scores: dict[str, float], portfolio: PortfolioContext
) -> str:
    """Deterministic user-message construction. Pure function -- fully
    unit-tested, no network/model call."""
    if not blended_scores:
        raise ValueError("build_portfolio_manager_prompt requires at least one blended score")

    scores_lines = "\n".join(
        f"  {symbol}: {score:+.1f}" for symbol, score in sorted(blended_scores.items())
    )

    if portfolio.holdings:
        holdings_lines = "\n".join(
            f"  {h.symbol}: {h.quantity} shares @ avg entry ${h.avg_entry_price:.2f}"
            for h in portfolio.holdings
        )
    else:
        holdings_lines = "  (no current holdings)"

    return (
        f"Blended signal scores:\n{scores_lines}\n\n"
        f"Portfolio context:\n"
        f"  Total portfolio value: ${portfolio.total_portfolio_value:,.2f}\n"
        f"  Cash available: ${portfolio.cash_available:,.2f}\n"
        f"Current holdings:\n{holdings_lines}"
    )


def parse_portfolio_manager_response(response_text: str) -> list[CandidateTradeProposal]:
    """Parse and validate the model's JSON reply into candidate trade
    proposals. Raises ValueError only for a systemic problem (unparseable
    JSON, or the root value isn't an array) -- an individual malformed
    proposal within an otherwise-good array is skipped and logged, not
    treated as a reason to discard every other proposal in the same
    response (see the per-item loop below). An empty array is valid and
    means "no trades this cycle" -- that's the model doing its job
    correctly, not an error.
    """
    text = response_text.strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    # Drop any invalid backslash escapes (see _fix_invalid_backslash_escapes)
    # before parsing -- a real, otherwise-well-formed response for the
    # closely-related news sentiment signal has been seen to contain one.
    text = _fix_invalid_backslash_escapes(text)

    # Skip any leading prose the model added despite instructions not to --
    # observed in practice on a wider (30-symbol) cycle with more to explain:
    # the model prefaced the array with a full sentence of reasoning before
    # the "[" ever appeared, which raw_decode can't handle on its own since
    # it anchors at the start of the string.
    array_start = text.find("[")
    if array_start > 0:
        text = text[array_start:]

    # Use raw_decode (rather than json.loads) so a model that appends stray
    # commentary after the JSON array despite instructions not to (observed
    # in practice) doesn't cause a hard parse failure -- we only need the
    # first complete JSON value in the string, and ignore anything after it.
    try:
        payload, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        # See the identical hint in news_sentiment.py's parse_sentiment_response
        # -- a parse error at the very end of the string means the response
        # got cut off before valid JSON completed, not a formatting mistake.
        hint = ""
        if exc.pos >= len(text):
            hint = " (response appears truncated -- got cut off before valid JSON completed; consider raising max_tokens)"
        raise ValueError(f"Portfolio manager response was not valid JSON{hint}: {response_text!r}") from exc

    if not isinstance(payload, list):
        raise ValueError(f"Portfolio manager response must be a JSON array, got: {response_text!r}")

    proposals: list[CandidateTradeProposal] = []
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            print(f"Portfolio manager proposal #{i} is not a JSON object -- skipping: {item!r}")
            continue

        symbol = item.get("symbol")
        side = item.get("side")
        quantity = item.get("quantity")
        reasoning = item.get("reasoning")

        # Each of these is a single malformed proposal, not a reason to
        # discard the whole batch -- e.g. a real response once included a
        # correct, well-formed proposal for every genuinely bullish symbol
        # AND one spurious zero-quantity placeholder for a bearish symbol
        # it held no shares of ("cannot short, so no action"), despite the
        # system prompt now explicitly forbidding that. Under the previous
        # all-or-nothing behavior, that single bad entry would have thrown
        # away every other good proposal in the same response.
        if not isinstance(symbol, str) or not symbol.strip():
            print(f"Portfolio manager proposal #{i} has missing/invalid 'symbol' -- skipping: {item!r}")
            continue
        if side not in ("buy", "sell"):
            print(f"Portfolio manager proposal #{i} has invalid 'side' -- skipping: {item!r}")
            continue
        if not isinstance(quantity, (int, float)) or quantity <= 0:
            print(f"Portfolio manager proposal #{i} has invalid 'quantity' (must be > 0) -- skipping: {item!r}")
            continue
        if not isinstance(reasoning, str) or not reasoning.strip():
            print(f"Portfolio manager proposal #{i} has missing/invalid 'reasoning' -- skipping: {item!r}")
            continue

        proposals.append(
            CandidateTradeProposal(
                symbol=symbol.strip().upper(),
                side=side,
                quantity=float(quantity),
                reasoning=reasoning,
            )
        )

    return proposals


# ============================================================
# LLM wrapper -- not unit-tested here (requires a live Anthropic API call).
# See tests/test_portfolio_manager.py for the skip-if-no-key fixture test.
# ============================================================


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


def propose_candidate_trades(
    blended_scores: dict[str, float],
    portfolio: PortfolioContext,
    anthropic_api_key: str,
    model: str = "claude-sonnet-5",
) -> list[CandidateTradeProposal]:
    """End-to-end: build the prompt, call the Anthropic API, parse the
    reply. Thin glue around the tested pure functions above."""
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    user_prompt = build_portfolio_manager_prompt(blended_scores, portfolio)

    # thinking disabled outright -- see the identical, root-caused fix and
    # full explanation in news_sentiment.py's score_news_sentiment. Claude
    # Sonnet 5 uses adaptive thinking that's on by default (confirmed via
    # Anthropic's docs) and its thinking tokens eat into the same max_tokens
    # budget as the visible JSON reply, which caused two real truncation
    # bugs in the sibling call. This is a structured trade-proposal task,
    # not one requiring step-by-step reasoning the deterministic risk/safety
    # layers downstream don't already re-verify, so disabling is safe here
    # too. max_tokens raised from 1000 defensively -- proposes trades across
    # potentially many symbols per cycle, so gets more headroom than the
    # single-symbol sentiment call even with thinking off.
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    response_text = _extract_response_text(response)
    return parse_portfolio_manager_response(response_text)
