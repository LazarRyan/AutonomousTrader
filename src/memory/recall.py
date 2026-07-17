"""
Recall -- the memory the portfolio manager sees BEFORE proposing trades.

This is the other half of the vault (src/memory/vault.py): reflection
writes memory at night, recall reads it back into the portfolio manager's
prompt every cycle. Before this existed, the agent was stateless -- it
re-discovered the same strong KHC signal three cycles in a row and bought
it all three times, because nothing told it that it had already acted
(confirmed in candidate_trades: five separate KHC adds over Jul 14-16 with
near-identical reasoning).

What goes into the memory context, and why each piece:

  1. Recent own actions per symbol (from candidate_trades, last N days):
     the direct fix for repeat-buying. The model sees "you already bought
     60 sh of KHC yesterday on this same signal" and the system prompt
     tells it a repeat action requires NEW information, not the same
     information re-observed.
  2. Open-position theses (from vault Positions/): why each holding is
     held and what would invalidate it -- so exits are proposed when a
     thesis breaks, not just when a blended score drifts negative.
  3. Lessons digest (vault Lessons.md, verbatim): the distilled rules the
     nightly reflection has learned from realized outcomes.
  4. Signal scorecard (vault Scorecard.md, verbatim): which signal sources
     have actually been predictive lately, so the model can weigh a
     momentum-driven score differently from an insider-driven one.

Same split as everywhere else:

  1. PURE, unit-tested: build_memory_context() and
     summarize_recent_actions() -- deterministic text assembly from typed
     inputs, no network. tests/test_recall.py.
  2. Thin glue: fetch_recent_actions() (one Supabase query) and
     gather_memory_context() (orchestrates the fetch + vault reads).
     Best-effort at the run_cycle call site: a memory failure must never
     stop a trading cycle -- the agent traded statelessly for weeks, so a
     single stateless cycle is a degradation, not an emergency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.memory.vault import Vault, read_lessons, read_position_thesis, read_scorecard

# How far back "recent actions" reaches. 7 calendar days spans a full
# trading week -- long enough to catch the repeat-buy pattern (which
# happened across consecutive days), short enough that stale month-old
# context doesn't crowd the prompt.
RECENT_ACTIONS_DAYS = 7

# Per-symbol cap on action lines injected into the prompt -- a symbol
# traded every cycle for a week would otherwise contribute ~15 lines alone.
MAX_ACTIONS_PER_SYMBOL = 6


@dataclass(frozen=True)
class RecentAction:
    """One past candidate trade, as recalled into the prompt. `status` is
    included so the model can distinguish 'I bought this' from 'I proposed
    this and risk blocked it' -- both are useful memory, but they mean
    different things for what to do next."""

    symbol: str
    side: str
    quantity: float
    status: str
    blended_signal_score: float | None
    reasoning: str
    created_at: str  # ISO timestamp string, as stored


def summarize_recent_actions(actions: list[RecentAction]) -> str:
    """Group recent actions by symbol into compact prompt lines, newest
    first within each symbol, capped at MAX_ACTIONS_PER_SYMBOL. Pure,
    unit-tested."""
    if not actions:
        return "  (no actions in the recall window)"

    by_symbol: dict[str, list[RecentAction]] = {}
    for action in actions:
        by_symbol.setdefault(action.symbol.upper(), []).append(action)

    lines: list[str] = []
    for symbol in sorted(by_symbol):
        symbol_actions = sorted(by_symbol[symbol], key=lambda a: a.created_at, reverse=True)
        for action in symbol_actions[:MAX_ACTIONS_PER_SYMBOL]:
            day = action.created_at[:10]
            score = f"{action.blended_signal_score:+.1f}" if action.blended_signal_score is not None else "n/a"
            lines.append(
                f"  {symbol}: {day} {action.side.upper()} x{action.quantity:g} "
                f"[{action.status}] (signal {score}) -- {action.reasoning}"
            )
        overflow = len(symbol_actions) - MAX_ACTIONS_PER_SYMBOL
        if overflow > 0:
            lines.append(f"  {symbol}: (+{overflow} earlier action(s) in window omitted)")
    return "\n".join(lines)


def build_memory_context(
    recent_actions: list[RecentAction],
    theses_by_symbol: dict[str, str],
    lessons_markdown: str,
    scorecard_markdown: str,
) -> str:
    """Assemble the full memory block appended to the portfolio manager's
    user prompt. Deterministic text assembly -- pure, unit-tested. Every
    section is always present (with an explicit 'none' placeholder) so the
    model never has to guess whether memory was unavailable or just empty."""
    if theses_by_symbol:
        theses_lines = "\n".join(
            f"  {symbol}: {thesis.strip().replace(chr(10), ' ')}" for symbol, thesis in sorted(theses_by_symbol.items())
        )
    else:
        theses_lines = "  (no theses on record)"

    lessons = lessons_markdown.strip() or "(no lessons recorded yet)"
    scorecard = scorecard_markdown.strip() or "(no scorecard yet -- signal sources unrated)"

    return (
        "Your memory (from your own past cycles -- use it, cite it in your reasoning):\n\n"
        f"Your recent actions (last {RECENT_ACTIONS_DAYS} days):\n{summarize_recent_actions(recent_actions)}\n\n"
        f"Open-position theses:\n{theses_lines}\n\n"
        f"Lessons learned from realized outcomes:\n{lessons}\n\n"
        f"Signal source scorecard:\n{scorecard}"
    )


# ============================================================
# Citation verification (2026-07-17, observability tier 1): the system
# prompt tells the model to cite a lesson when it changes a decision, and
# the ops metrics count those citations -- but a count trusts the model's
# word. This closes the loop deterministically: when reasoning CLAIMS a
# lesson, check that some actual vault lesson shares enough distinctive
# vocabulary with the reasoning to plausibly be the one cited. Cheap
# string math, no LLM -- the fuzzy remainder (was the cited lesson
# genuinely relevant?) is future judge-eval territory, but "did the cited
# lesson exist at all" should never cost tokens to answer.
# ============================================================

import re as _re

# Words too common in this domain to indicate WHICH lesson is being cited.
_CITATION_STOPWORDS = {
    "the", "and", "for", "not", "with", "was", "were", "that", "this", "than",
    "but", "its", "are", "has", "have", "had", "when", "then", "will", "would",
    "signal", "signals", "score", "scores", "position", "positions", "trade",
    "trades", "trading", "buy", "sell", "bullish", "bearish", "lesson", "lessons",
}

_LESSON_LINE_PREFIX_RE = _re.compile(r"^- \[\d{4}-\d{2}-\d{2}\]\s*")


def _content_words(text: str) -> set[str]:
    return {w for w in _re.findall(r"[a-z]{3,}", text.lower()) if w not in _CITATION_STOPWORDS}


def verify_lesson_citation(reasoning: str, lessons_markdown: str, min_overlap: float = 0.35) -> bool | None:
    """None: the reasoning doesn't claim a lesson (nothing to verify).
    True: it claims one, and an actual vault lesson shares >= min_overlap
    of its distinctive vocabulary with the reasoning. False: it claims one
    no vault lesson plausibly matches -- a fabricated or garbled citation,
    which is exactly the failure mode worth surfacing. Pure, unit-tested."""
    if "lesson" not in reasoning.lower():
        return None

    lesson_lines = [
        _LESSON_LINE_PREFIX_RE.sub("", line)
        for line in lessons_markdown.splitlines()
        if line.startswith("- [")
    ]
    if not lesson_lines:
        return False  # cites a lesson while the vault has none -- fabricated by definition

    reasoning_words = _content_words(reasoning)
    for lesson in lesson_lines:
        lesson_words = _content_words(lesson)
        if not lesson_words:
            continue
        if len(lesson_words & reasoning_words) / len(lesson_words) >= min_overlap:
            return True
    return False


# ============================================================
# Thin glue -- one Supabase query + vault file reads.
# ============================================================


def fetch_recent_actions(supabase_client, days: int = RECENT_ACTIONS_DAYS) -> list[RecentAction]:
    """All candidate_trades rows from the last `days` days -- proposals
    included, not just executions (see RecentAction.status docstring)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        supabase_client.table("candidate_trades")
        .select("symbol, side, quantity, status, blended_signal_score, portfolio_manager_reasoning, created_at")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .execute()
    )
    return [
        RecentAction(
            symbol=row["symbol"],
            side=row["side"],
            quantity=float(row["quantity"]),
            status=row["status"],
            blended_signal_score=(
                float(row["blended_signal_score"]) if row.get("blended_signal_score") is not None else None
            ),
            reasoning=row.get("portfolio_manager_reasoning") or "",
            created_at=row["created_at"],
        )
        for row in (result.data or [])
    ]


def gather_memory_context(supabase_client, vault: Vault, held_symbols: list[str]) -> str:
    """Fetch + assemble the full memory context for one cycle. Raises on
    the Supabase query failing (caller decides how tolerant to be -- see
    run_cycle's best-effort wiring); vault reads of missing files are just
    empty sections, not errors."""
    recent_actions = fetch_recent_actions(supabase_client)
    theses = {}
    for symbol in held_symbols:
        thesis = read_position_thesis(vault, symbol)
        if thesis:
            theses[symbol.upper()] = thesis
    return build_memory_context(
        recent_actions,
        theses,
        read_lessons(vault),
        read_scorecard(vault),
    )
