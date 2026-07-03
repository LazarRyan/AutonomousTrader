"""
Diagnostic for the recurring news-sentiment JSON truncation bug.

Two live dry runs now show the exact same failure signature (response cut
off right after the "reasoning" string closes, no final "}") even AFTER
thinking={"type": "disabled"} was added specifically to eliminate this
(commit 5fe08d5). That fix should have worked per Anthropic's docs, so
something about the assumption is wrong -- rather than guess again (raise
max_tokens further? thinking not actually being honored?), this pulls the
FULL raw response metadata so we can see exactly what happened server-side:

  - stop_reason ("end_turn" = model finished normally and just didn't emit
    the closing brace; "max_tokens" = genuinely still truncated)
  - usage (input/output tokens, and any thinking-token breakdown if the SDK
    surfaces one)
  - number and type of every content block (in case thinking is still
    sneaking in as a block even with "disabled" set)
  - the raw text, printed in full with an explicit length, so we can see
    exactly where it stops

Run this by hand (from the project root, with your real .env loaded) for
MMM -- the symbol that failed most recently:

    python scripts/debug_sentiment_truncation.py MMM

Paste the full output back -- that will tell us definitively what's going
on rather than adding another speculative fix.
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")  # allow `python scripts/debug_sentiment_truncation.py` from repo root

from src.config import load_settings
from src.signals.news_sentiment import _SYSTEM_PROMPT, build_sentiment_prompt, fetch_recent_news


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python scripts/debug_sentiment_truncation.py SYMBOL")
        sys.exit(1)

    symbol = sys.argv[1].upper()
    settings = load_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY must be set")

    import anthropic

    print(f"anthropic SDK version: {anthropic.__version__}")

    headlines = fetch_recent_news(symbol, settings.alpaca_api_key, settings.alpaca_secret_key)
    if not headlines:
        print(f"No recent headlines for {symbol} -- pick a symbol that failed in a real run instead.")
        sys.exit(1)

    print(f"Fetched {len(headlines)} headlines for {symbol}:")
    for h in headlines:
        print(f"  - {h}")
    print()

    user_prompt = build_sentiment_prompt(symbol, headlines)
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user_prompt}],
    )

    print("=" * 70)
    print(f"stop_reason: {response.stop_reason}")
    print(f"usage: {response.usage}")
    print(f"number of content blocks: {len(response.content)}")
    for i, block in enumerate(response.content):
        block_type = getattr(block, "type", "?")
        print(f"  block[{i}] type={block_type}")
        if block_type == "thinking":
            thinking_text = getattr(block, "thinking", "")
            print(f"    thinking block present despite thinking=disabled! length={len(thinking_text)}")
            print(f"    thinking content: {thinking_text!r}")
        elif block_type == "text":
            print(f"    text length: {len(block.text)}")
            print(f"    text (full, repr): {block.text!r}")
    print("=" * 70)


if __name__ == "__main__":
    main()
