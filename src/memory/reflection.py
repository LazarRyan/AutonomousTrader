"""
Nightly reflection -- where trading history becomes memory.

Runs once per trading day after the close (src/nightly.py). Reads what
actually happened -- every fill, and crucially every CLOSED lot's realized
P&L matched back to the reasoning that opened it -- and writes the three
memory surfaces the next morning's recall step feeds back into the
portfolio manager prompt:

  1. A journal section for the day (vault Journal/YYYY-MM-DD.md).
  2. Distilled lessons (vault Lessons.md) -- only from REALIZED outcomes.
     A lesson is "entry reasoning X led to realized loss Y under
     conditions Z", never "the model feels it should be more careful".
     Realized-only is deliberate: unrealized P&L swings daily and would
     teach whipsaw; a closed lot is a finished experiment.
  3. Refreshed per-position theses (vault Positions/<SYM>.md) for every
     currently-open holding -- why it's held NOW and what would invalidate
     it, so tomorrow's cycles can propose thesis-driven exits.

The LLM is used for exactly one thing: distillation (turning matched
outcomes into short transferable rules and theses). Everything factual --
FIFO lot matching, realized P&L arithmetic, what traded today -- is
deterministic and unit-tested; the model can't get the numbers wrong
because it never computes them, it only explains them.

Split:
  1. PURE, unit-tested (tests/test_reflection.py): match_closed_lots()
     (FIFO lot matching + realized P&L), build_reflection_prompt(),
     parse_reflection_response().
  2. Thin glue: run_reflection() -- Supabase reads, one Anthropic call,
     vault writes. Vault writes here are LOUD (unlike run_cycle's
     best-effort memory reads): writing memory is this job's entire
     purpose, so a failure must surface in the launchd log, not vanish.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.agents.portfolio_manager import _extract_response_text, _fix_invalid_backslash_escapes

# Cap on how many lessons one night can add -- a bad day shouldn't flood
# Lessons.md (which is prompt-injected every cycle) with ten variations of
# the same regret. The model is told the cap and asked to prioritize.
MAX_LESSONS_PER_NIGHT = 3


@dataclass(frozen=True)
class ExecutionRecord:
    """One executed_trades row joined with its candidate's reasoning."""

    symbol: str
    side: str
    quantity: float
    fill_price: float | None
    executed_at: datetime
    reasoning: str
    blended_signal_score: float | None


@dataclass(frozen=True)
class ClosedLot:
    """One FIFO-matched round trip: some quantity bought, later sold."""

    symbol: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    entry_reasoning: str
    exit_reasoning: str

    @property
    def realized_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def realized_pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price


@dataclass(frozen=True)
class OpenPositionSummary:
    symbol: str
    quantity: float
    avg_entry_price: float
    current_price: float | None
    unrealized_pnl_pct: float | None


@dataclass(frozen=True)
class ReflectionResult:
    journal_summary: str
    lessons: list[str] = field(default_factory=list)
    theses_by_symbol: dict[str, str] = field(default_factory=dict)


def match_closed_lots(executions: list[ExecutionRecord]) -> list[ClosedLot]:
    """FIFO-match sells against prior buys per symbol to produce closed
    lots with realized P&L. Pure, unit-tested.

    Records with no fill_price are skipped (no honest P&L without a real
    price -- executed_trades.fill_price is nullable for orders whose fill
    confirmation never arrived, and inventing a price would corrupt every
    lesson downstream). A sell with no prior recorded buy inventory (e.g.
    a position opened before executed_trades existed) is skipped for the
    unmatched portion, same reasoning."""
    lots: list[ClosedLot] = []
    # Per symbol: open buy inventory as [remaining_qty, price, at, reasoning]
    inventory: dict[str, list[list]] = {}

    for record in sorted(executions, key=lambda r: r.executed_at):
        if record.fill_price is None:
            continue
        symbol = record.symbol.upper()
        if record.side == "buy":
            inventory.setdefault(symbol, []).append(
                [record.quantity, record.fill_price, record.executed_at, record.reasoning]
            )
            continue

        remaining_to_match = record.quantity
        queue = inventory.get(symbol, [])
        while remaining_to_match > 1e-9 and queue:
            lot = queue[0]
            matched = min(lot[0], remaining_to_match)
            lots.append(
                ClosedLot(
                    symbol=symbol,
                    quantity=matched,
                    entry_price=lot[1],
                    exit_price=record.fill_price,
                    entry_at=lot[2],
                    exit_at=record.executed_at,
                    entry_reasoning=lot[3],
                    exit_reasoning=record.reasoning,
                )
            )
            lot[0] -= matched
            remaining_to_match -= matched
            if lot[0] <= 1e-9:
                queue.pop(0)

    return lots


def build_reflection_prompt(
    day: date,
    todays_executions: list[ExecutionRecord],
    closed_lots: list[ClosedLot],
    open_positions: list[OpenPositionSummary],
    existing_lessons: str,
) -> str:
    """Deterministic prompt assembly. Pure, unit-tested."""
    if todays_executions:
        executions_lines = "\n".join(
            f"  {e.executed_at.date().isoformat()} {e.symbol} {e.side.upper()} x{e.quantity:g}"
            + (f" @ ${e.fill_price:.2f}" if e.fill_price is not None else " @ (no fill price)")
            + f" -- entry reasoning: {e.reasoning}"
            for e in todays_executions
        )
    else:
        executions_lines = "  (no executions today)"

    if closed_lots:
        lots_lines = "\n".join(
            f"  {lot.symbol}: {lot.quantity:g} sh, ${lot.entry_price:.2f} -> ${lot.exit_price:.2f} "
            f"({lot.realized_pnl_pct:+.2%}, ${lot.realized_pnl:+,.2f}), held {(lot.exit_at - lot.entry_at).days}d. "
            f"OPENED because: {lot.entry_reasoning} CLOSED because: {lot.exit_reasoning}"
            for lot in closed_lots
        )
    else:
        lots_lines = "  (no lots closed recently -- no new realized outcomes to learn from)"

    if open_positions:
        positions_lines = "\n".join(
            f"  {p.symbol}: {p.quantity:g} sh @ avg ${p.avg_entry_price:.2f}"
            + (
                f", now ${p.current_price:.2f} ({p.unrealized_pnl_pct:+.2%} unrealized)"
                if p.current_price is not None and p.unrealized_pnl_pct is not None
                else ""
            )
            for p in open_positions
        )
    else:
        positions_lines = "  (no open positions)"

    return (
        f"Today is {day.isoformat()}. Reflect on this paper-trading account's realized outcomes.\n\n"
        f"Today's executions:\n{executions_lines}\n\n"
        f"Recently closed lots (realized outcomes -- the only basis for lessons):\n{lots_lines}\n\n"
        f"Current open positions:\n{positions_lines}\n\n"
        f"Existing lessons (do NOT repeat these -- refine or supersede only if today's evidence warrants):\n"
        f"{existing_lessons.strip() or '(none yet)'}"
    )


_REFLECTION_SYSTEM_PROMPT = f"""You are the nightly reflection step of an automated paper-trading system.
You are given today's executions, recently CLOSED lots with realized P&L matched to the exact reasoning
that opened and closed them, and the current open positions.

Produce:
1. "journal_summary": 2-4 sentences summarizing the day's trading honestly (including "quiet day" if so).
2. "lessons": 0 to {MAX_LESSONS_PER_NIGHT} short, transferable rules derived ONLY from realized outcomes
   shown to you (closed lots). Each lesson must reference the concrete evidence (symbol, rough P&L) that
   produced it. If there are no closed lots, or the closed lots teach nothing new beyond the existing
   lessons, return an empty array -- fabricating lessons from unrealized moves or hunches is worse than
   silence. A lesson about a signal source being unreliable is valid if the evidence shows it.
3. "theses": for EVERY current open position, one or two sentences: why this position is worth holding
   now, and what specific observable condition should trigger an exit. Base it on the entry reasoning and
   current P&L shown -- do not invent facts about the companies.

Your ENTIRE reply must be a single JSON object and nothing else -- the very first character must be "{{":
{{"journal_summary": "<text>", "lessons": ["<rule>"], "theses": {{"<TICKER>": "<thesis>"}}}}
"""


def parse_reflection_response(response_text: str) -> ReflectionResult:
    """Parse/validate the model's JSON reply. Same tolerance toolkit as
    parse_portfolio_manager_response (fenced-code stripping, invalid-escape
    repair, leading-prose skip, raw_decode for trailing prose) -- same
    model, same known failure modes. Pure, unit-tested."""
    text = response_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    text = _fix_invalid_backslash_escapes(text)
    object_start = text.find("{")
    if object_start > 0:
        text = text[object_start:]

    try:
        payload, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reflection response was not valid JSON: {response_text!r}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Reflection response must be a JSON object, got: {response_text!r}")

    journal_summary = payload.get("journal_summary")
    if not isinstance(journal_summary, str) or not journal_summary.strip():
        raise ValueError("Reflection response missing 'journal_summary'")

    lessons_raw = payload.get("lessons", [])
    if not isinstance(lessons_raw, list):
        raise ValueError("Reflection 'lessons' must be an array")
    lessons = [item.strip() for item in lessons_raw if isinstance(item, str) and item.strip()]
    lessons = lessons[:MAX_LESSONS_PER_NIGHT]  # enforce the cap even if the model ignores it

    theses_raw = payload.get("theses", {})
    if not isinstance(theses_raw, dict):
        raise ValueError("Reflection 'theses' must be an object")
    theses = {
        symbol.strip().upper(): thesis.strip()
        for symbol, thesis in theses_raw.items()
        if isinstance(symbol, str) and isinstance(thesis, str) and symbol.strip() and thesis.strip()
    }

    return ReflectionResult(journal_summary=journal_summary.strip(), lessons=lessons, theses_by_symbol=theses)


# ============================================================
# Thin glue -- Supabase reads, one Anthropic call, vault writes.
# ============================================================


def fetch_execution_history(supabase_client, days: int = 90) -> list[ExecutionRecord]:
    """executed_trades joined with each row's candidate reasoning. 90 days
    so FIFO matching has the full entry-side history for anything closed
    recently."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = (
        supabase_client.table("executed_trades")
        .select("symbol, side, quantity, fill_price, executed_at, candidate_trades(portfolio_manager_reasoning, blended_signal_score)")
        .gte("executed_at", cutoff)
        .order("executed_at")
        .execute()
        .data
        or []
    )
    records = []
    for row in rows:
        candidate = row.get("candidate_trades") or {}
        records.append(
            ExecutionRecord(
                symbol=row["symbol"],
                side=row["side"],
                quantity=float(row["quantity"]),
                fill_price=float(row["fill_price"]) if row.get("fill_price") is not None else None,
                executed_at=datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00")),
                reasoning=candidate.get("portfolio_manager_reasoning") or "",
                blended_signal_score=(
                    float(candidate["blended_signal_score"])
                    if candidate.get("blended_signal_score") is not None
                    else None
                ),
            )
        )
    return records


def run_reflection(supabase_client, alpaca_trading_client, settings, vault, today: date | None = None) -> ReflectionResult:
    """The nightly entry (called by src/nightly.py): gather -> distill ->
    write memory. Returns the result so the newsletter can embed it."""
    import anthropic

    from src.memory.vault import append_journal_entry, append_lessons, read_lessons, upsert_position_note

    today = today or datetime.now(timezone.utc).date()

    history = fetch_execution_history(supabase_client)
    todays_executions = [e for e in history if e.executed_at.date() == today]
    all_lots = match_closed_lots(history)
    # Lessons come only from lots closed in the last 3 days -- older closes
    # were already reflected on the night they happened.
    recent_lots = [lot for lot in all_lots if (today - lot.exit_at.date()).days <= 3]

    positions = alpaca_trading_client.get_all_positions()
    open_positions = [
        OpenPositionSummary(
            symbol=p.symbol,
            quantity=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price) if getattr(p, "current_price", None) is not None else None,
            unrealized_pnl_pct=float(p.unrealized_plpc) if getattr(p, "unrealized_plpc", None) is not None else None,
        )
        for p in positions
    ]

    prompt = build_reflection_prompt(today, todays_executions, recent_lots, open_positions, read_lessons(vault))

    from src.llm_metering import make_recorder

    record_llm_call = make_recorder(supabase_client, "reflection")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # thinking disabled + retry: same root-caused pattern as
    # propose_candidate_trades -- see the long comment there.
    last_error: ValueError | None = None
    result: ReflectionResult | None = None
    for attempt in range(1, 3):
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            system=_REFLECTION_SYSTEM_PROMPT,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        record_llm_call("claude-sonnet-5", response.usage)
        try:
            result = parse_reflection_response(_extract_response_text(response))
            break
        except ValueError as exc:
            last_error = exc
            print(f"Reflection response attempt {attempt}/2 failed to parse: {exc}")
    if result is None:
        assert last_error is not None
        raise last_error

    # Write memory -- loudly (see module docstring).
    realized_lines = "\n".join(
        f"- {lot.symbol}: {lot.realized_pnl_pct:+.2%} (${lot.realized_pnl:+,.2f}) on {lot.quantity:g} sh, "
        f"held {(lot.exit_at - lot.entry_at).days}d"
        for lot in recent_lots
    )
    journal_body = result.journal_summary
    if realized_lines:
        journal_body += f"\n\nRealized outcomes reflected on tonight:\n{realized_lines}"
    if result.lessons:
        journal_body += "\n\nLessons distilled tonight:\n" + "\n".join(f"- {lesson}" for lesson in result.lessons)
    append_journal_entry(vault, today, "Nightly reflection", journal_body)

    append_lessons(vault, today, result.lessons)

    open_symbols = {p.symbol.upper() for p in open_positions}
    for symbol, thesis in result.theses_by_symbol.items():
        if symbol in open_symbols:  # never write a thesis for a position we don't hold
            upsert_position_note(vault, symbol, thesis, history_line=f"{today.isoformat()}: thesis refreshed by nightly reflection")

    return result
