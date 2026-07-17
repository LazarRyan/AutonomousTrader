from datetime import date

from src.memory.vault import (
    MAX_LESSONS,
    Vault,
    _merge_lessons,
    append_journal_entry,
    append_lessons,
    append_position_history,
    parse_position_note_thesis,
    read_journal,
    read_lessons,
    read_position_thesis,
    read_scorecard,
    render_position_note,
    upsert_position_note,
    write_scorecard,
)


class TestJournal:
    def test_first_write_creates_titled_note(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        append_journal_entry(vault, date(2026, 7, 16), "Cycle at 09:20 ET", "Bought nothing.")
        text = read_journal(vault, date(2026, 7, 16))
        assert text.startswith("# Trading Journal — 2026-07-16")
        assert "## Cycle at 09:20 ET" in text
        assert "Bought nothing." in text

    def test_same_day_sections_append(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        day = date(2026, 7, 16)
        append_journal_entry(vault, day, "Cycle at 09:20 ET", "a")
        append_journal_entry(vault, day, "Nightly reflection", "b")
        text = read_journal(vault, day)
        assert text.index("Cycle at 09:20 ET") < text.index("Nightly reflection")
        assert text.count("# Trading Journal") == 1

    def test_missing_day_reads_empty(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        assert read_journal(vault, date(2026, 1, 1)) == ""


class TestPositionNotes:
    def test_render_and_parse_round_trip(self):
        note = render_position_note("khc", "Cheap staples turnaround. Exit if signal < -10.", ["2026-07-15: BUY x60"])
        assert note.startswith("# KHC")
        assert parse_position_note_thesis(note) == "Cheap staples turnaround. Exit if signal < -10."

    def test_parse_missing_or_empty_thesis_is_none(self):
        assert parse_position_note_thesis("# KHC\n\n## History\n\n- x\n") is None
        assert parse_position_note_thesis(render_position_note("KHC", "", [])) is None

    def test_upsert_preserves_history_and_replaces_thesis(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        upsert_position_note(vault, "KHC", "old thesis", history_line="2026-07-14: BUY x20")
        upsert_position_note(vault, "KHC", "new thesis", history_line="2026-07-15: BUY x60")
        text = vault.position_path("KHC").read_text()
        assert read_position_thesis(vault, "KHC") == "new thesis"
        assert "old thesis" not in text
        assert "2026-07-14: BUY x20" in text
        assert "2026-07-15: BUY x60" in text

    def test_append_history_without_thesis_uses_placeholder(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        append_position_history(vault, "GE", "2026-07-15: BUY x10")
        assert "pending nightly reflection" in (read_position_thesis(vault, "GE") or "")
        assert "2026-07-15: BUY x10" in vault.position_path("GE").read_text()

    def test_missing_note_thesis_is_none(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        assert read_position_thesis(vault, "ZZZZ") is None


class TestLessons:
    def test_append_and_read(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        append_lessons(vault, date(2026, 7, 16), ["Momentum whipsawed on IBM; wait for confirmation."])
        text = read_lessons(vault)
        assert "- [2026-07-16] Momentum whipsawed on IBM; wait for confirmation." in text

    def test_newest_first_and_dedupe_replaces_older_copy(self):
        merged = _merge_lessons(
            ["- [2026-07-10] rule A", "- [2026-07-09] rule B"],
            [(date(2026, 7, 16), "rule B")],
        )
        assert merged[0] == "- [2026-07-16] rule B"
        assert "- [2026-07-09] rule B" not in merged
        assert "- [2026-07-10] rule A" in merged

    def test_cap_at_max_lessons(self):
        existing = [f"- [2026-06-01] old rule {i}" for i in range(MAX_LESSONS)]
        merged = _merge_lessons(existing, [(date(2026, 7, 16), "brand new rule")])
        assert len(merged) == MAX_LESSONS
        assert merged[0].endswith("brand new rule")

    def test_empty_lessons_write_nothing(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        append_lessons(vault, date(2026, 7, 16), [])
        assert read_lessons(vault) == ""


class TestScorecard:
    def test_round_trip_and_missing_reads_empty(self, tmp_path):
        vault = Vault(root=tmp_path / "vault")
        assert read_scorecard(vault) == ""
        write_scorecard(vault, "# Signal Source Scorecard\n\n| a |\n")
        assert "Signal Source Scorecard" in read_scorecard(vault)
