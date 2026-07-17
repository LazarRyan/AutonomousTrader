"""
Obsidian-compatible memory vault -- plain .md files under vault/ in the
repo root. This is the agent's persistent memory: open the folder as an
Obsidian vault and every note renders/links natively, but nothing here
depends on Obsidian existing -- it's just markdown files on disk.

Layout:

    vault/
      Journal/2026-07-16.md   one note per day: every cycle's decisions
      Positions/KHC.md        one note per symbol: thesis + action history
      Lessons.md              distilled post-trade lessons (append-only,
                              newest first, capped -- see append_lesson)
      Scorecard.md            rolling per-signal-source accuracy, rewritten
                              by the weight tuner (src/signals/weight_tuner.py)
      Newsletters/            nightly newsletter copies (src/newsletter.py)

Why files and not another Supabase table: the whole point is that Ryan can
open this in Obsidian and read what the agent has learned, and the agent's
recall step (src/memory/recall.py) can hand the LLM plain text without a
query layer in between. Supabase remains the source of truth for the
decision AUDIT trail (audit_log/candidate_trades); the vault is the
DISTILLED memory layered on top -- losing it would lose learned lessons but
never the underlying facts, which can regenerate it.

Same split as the rest of the project:

  1. PURE, unit-tested: the markdown rendering/parsing helpers
     (render_position_note, parse_position_note_thesis, lesson trimming in
     append_lesson's pure core _merge_lessons).
  2. Thin file-IO glue: the read_*/write_*/append_* functions -- simple
     open/read/write with directories created on demand. Deliberately
     best-effort at call sites in the trading cycle (a vault write failure
     must never block a trade decision -- see run_cycle's wiring), but loud
     in the nightly reflection job where writing memory IS the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Repo root is two parents up from this file (src/memory/vault.py).
DEFAULT_VAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "vault"

# Lessons.md is what gets injected into the portfolio manager prompt every
# cycle, so it must stay small enough to never crowd out the actual signal
# data. Oldest lessons fall off the end; the nightly reflection is expected
# to re-distill recurring themes into fewer, stronger lessons over time.
MAX_LESSONS = 30


@dataclass(frozen=True)
class Vault:
    """All vault paths derived from one root, so tests can point the whole
    thing at a tmp_path and the production default stays a one-liner."""

    root: Path = DEFAULT_VAULT_ROOT

    @property
    def journal_dir(self) -> Path:
        return self.root / "Journal"

    @property
    def positions_dir(self) -> Path:
        return self.root / "Positions"

    @property
    def newsletters_dir(self) -> Path:
        return self.root / "Newsletters"

    @property
    def lessons_path(self) -> Path:
        return self.root / "Lessons.md"

    @property
    def scorecard_path(self) -> Path:
        return self.root / "Scorecard.md"

    def journal_path(self, day: date) -> Path:
        return self.journal_dir / f"{day.isoformat()}.md"

    def position_path(self, symbol: str) -> Path:
        return self.positions_dir / f"{symbol.upper()}.md"

    def newsletter_path(self, day: date) -> Path:
        return self.newsletters_dir / f"{day.isoformat()}.md"


# ============================================================
# Journal -- one note per day, appended to by every cycle and by the
# nightly reflection. Append-only within a day so three scheduled cycles
# plus the nightly job each add their own timestamped section.
# ============================================================


def append_journal_entry(vault: Vault, day: date, heading: str, body_markdown: str) -> None:
    """Append one '## <heading>' section to the day's journal note,
    creating the note (with a title line) on first write of the day."""
    vault.journal_dir.mkdir(parents=True, exist_ok=True)
    path = vault.journal_path(day)
    if not path.exists():
        path.write_text(f"# Trading Journal — {day.isoformat()}\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"## {heading}\n\n{body_markdown.rstrip()}\n\n")


def read_journal(vault: Vault, day: date) -> str:
    path = vault.journal_path(day)
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ============================================================
# Position notes -- one per symbol. The '## Thesis' section is the part
# recall injects into the portfolio manager prompt: WHY the position is
# held, what would invalidate it, and when it was last revised. The
# '## History' section is an append-only action log.
# ============================================================

_THESIS_SECTION_RE = re.compile(r"^## Thesis\s*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def render_position_note(symbol: str, thesis_markdown: str, history_lines: list[str]) -> str:
    """Pure renderer for a full position note. Unit-tested."""
    history = "\n".join(f"- {line}" for line in history_lines) if history_lines else "- (no actions recorded yet)"
    return (
        f"# {symbol.upper()}\n\n"
        f"## Thesis\n\n{thesis_markdown.strip()}\n\n"
        f"## History\n\n{history}\n"
    )


def parse_position_note_thesis(note_text: str) -> str | None:
    """Extract the '## Thesis' section body from a position note. Returns
    None if the note has no thesis section (or it's empty) -- callers treat
    'no thesis on record' as a real, meaningful state (e.g. a position that
    was opened before the vault existed), never as an error. Pure,
    unit-tested."""
    match = _THESIS_SECTION_RE.search(note_text)
    if not match:
        return None
    thesis = match.group(1).strip()
    return thesis or None


# The machine-readable exit-target line the nightly reflection appends to
# a thesis (see reflection.format_thesis_with_targets). Lives inside the
# thesis text on purpose: the portfolio manager sees targets as part of
# the thesis it reads, Obsidian shows them where a human looks for them,
# and this one regex is the entire "schema".
_EXIT_TARGETS_RE = re.compile(
    r"^Exit targets:(?:\s+above \$(?P<above>[\d,]+(?:\.\d+)?))?(?:\s*·\s*)?(?:\s*below \$(?P<below>[\d,]+(?:\.\d+)?))?\s*$",
    re.MULTILINE,
)


def parse_exit_targets(thesis_text: str) -> tuple[float | None, float | None]:
    """(exit_above, exit_below) from a thesis's 'Exit targets:' line, or
    (None, None) when the thesis has no targets -- an old-format thesis is
    a thesis without targets, never an error. Pure, unit-tested."""
    match = _EXIT_TARGETS_RE.search(thesis_text)
    if not match:
        return None, None

    def _num(raw: str | None) -> float | None:
        return float(raw.replace(",", "")) if raw else None

    return _num(match.group("above")), _num(match.group("below"))


def read_position_thesis(vault: Vault, symbol: str) -> str | None:
    path = vault.position_path(symbol)
    if not path.exists():
        return None
    return parse_position_note_thesis(path.read_text(encoding="utf-8"))


def upsert_position_note(vault: Vault, symbol: str, thesis_markdown: str, history_line: str | None = None) -> None:
    """Set/replace the thesis for a symbol and optionally append one history
    line, preserving existing history. Creating the note if absent."""
    vault.positions_dir.mkdir(parents=True, exist_ok=True)
    path = vault.position_path(symbol)

    history_lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- ") and "(no actions recorded yet)" not in line:
                history_lines.append(line[2:])
    if history_line:
        history_lines.append(history_line)

    path.write_text(render_position_note(symbol, thesis_markdown, history_lines), encoding="utf-8")


def append_position_history(vault: Vault, symbol: str, history_line: str) -> None:
    """Append one action line to a symbol's history, keeping whatever thesis
    is already on record (or a placeholder if the note is new -- the nightly
    reflection is responsible for writing real theses)."""
    existing_thesis = read_position_thesis(vault, symbol) or "_No thesis recorded yet -- pending nightly reflection._"
    upsert_position_note(vault, symbol, existing_thesis, history_line)


# ============================================================
# Lessons -- the distilled learning. Injected into every portfolio manager
# prompt, so kept deliberately small (MAX_LESSONS).
# ============================================================

_LESSON_LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\] (.+)$")


def _merge_lessons(existing_lines: list[str], new_lessons: list[tuple[date, str]]) -> list[str]:
    """Pure core of append_lesson: prepend new dated lessons, dedupe exact
    repeats (the same lesson re-learned on a later date replaces the older
    entry rather than stacking -- recency matters more than repetition
    count here, since the DATE is what tells the reader/model how current
    a rule is), cap at MAX_LESSONS. Unit-tested."""
    merged: list[str] = [f"- [{d.isoformat()}] {text.strip()}" for d, text in new_lessons]
    new_texts = {text.strip() for _, text in new_lessons}
    for line in existing_lines:
        match = _LESSON_LINE_RE.match(line)
        if match and match.group(2).strip() in new_texts:
            continue  # superseded by the newer copy of the same lesson
        merged.append(line)
    return merged[:MAX_LESSONS]


def read_lessons(vault: Vault) -> str:
    """The full Lessons.md text (empty string if none yet) -- this is what
    recall hands to the portfolio manager prompt verbatim."""
    if not vault.lessons_path.exists():
        return ""
    return vault.lessons_path.read_text(encoding="utf-8")


def append_lessons(vault: Vault, day: date, lessons: list[str]) -> None:
    """Add newly-distilled lessons (newest first) to Lessons.md."""
    if not lessons:
        return
    vault.root.mkdir(parents=True, exist_ok=True)

    existing_lines: list[str] = []
    if vault.lessons_path.exists():
        existing_lines = [
            line for line in vault.lessons_path.read_text(encoding="utf-8").splitlines() if line.startswith("- [")
        ]

    merged = _merge_lessons(existing_lines, [(day, lesson) for lesson in lessons])
    header = (
        "# Lessons\n\n"
        "Distilled by the nightly reflection from realized trade outcomes. "
        f"Newest first, capped at {MAX_LESSONS} -- recurring themes should be "
        "re-distilled into fewer, stronger rules over time.\n\n"
    )
    vault.lessons_path.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")


# ============================================================
# Scorecard -- rewritten wholesale by the weight tuner; read verbatim by
# recall. No parsing needed on the read side, it's prompt text.
# ============================================================


def read_scorecard(vault: Vault) -> str:
    if not vault.scorecard_path.exists():
        return ""
    return vault.scorecard_path.read_text(encoding="utf-8")


def write_scorecard(vault: Vault, markdown: str) -> None:
    vault.root.mkdir(parents=True, exist_ok=True)
    vault.scorecard_path.write_text(markdown, encoding="utf-8")


def write_newsletter(vault: Vault, day: date, markdown: str) -> None:
    vault.newsletters_dir.mkdir(parents=True, exist_ok=True)
    vault.newsletter_path(day).write_text(markdown, encoding="utf-8")
