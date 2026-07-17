from src.memory.recall import (
    MAX_ACTIONS_PER_SYMBOL,
    RecentAction,
    build_memory_context,
    summarize_recent_actions,
    verify_lesson_citation,
)

LESSONS_MD = (
    "# Lessons\n\n"
    "- [2026-07-16] Do not add to a position on an unchanged momentum reading; "
    "wait for confirmation from a second source.\n"
    "- [2026-07-15] Congressional filings alone proved unreliable for KHC-style staples names.\n"
)


def make_action(**overrides) -> RecentAction:
    defaults = dict(
        symbol="KHC",
        side="buy",
        quantity=20.0,
        status="executed",
        blended_signal_score=38.4,
        reasoning="strong signal, adding",
        created_at="2026-07-15T14:30:00+00:00",
    )
    defaults.update(overrides)
    return RecentAction(**defaults)


class TestSummarizeRecentActions:
    def test_empty_gets_explicit_placeholder(self):
        assert "no actions in the recall window" in summarize_recent_actions([])

    def test_one_action_renders_all_fields(self):
        text = summarize_recent_actions([make_action()])
        assert "KHC: 2026-07-15 BUY x20 [executed] (signal +38.4) -- strong signal, adding" in text

    def test_missing_score_renders_na(self):
        text = summarize_recent_actions([make_action(blended_signal_score=None)])
        assert "(signal n/a)" in text

    def test_per_symbol_cap_with_overflow_note(self):
        actions = [
            make_action(created_at=f"2026-07-{10 + i:02d}T14:00:00+00:00") for i in range(MAX_ACTIONS_PER_SYMBOL + 3)
        ]
        text = summarize_recent_actions(actions)
        assert text.count("KHC: 2026-07-") == MAX_ACTIONS_PER_SYMBOL
        assert "(+3 earlier action(s) in window omitted)" in text

    def test_newest_first_within_symbol(self):
        actions = [
            make_action(created_at="2026-07-10T14:00:00+00:00", reasoning="older"),
            make_action(created_at="2026-07-15T14:00:00+00:00", reasoning="newer"),
        ]
        text = summarize_recent_actions(actions)
        assert text.index("newer") < text.index("older")


class TestBuildMemoryContext:
    def test_all_sections_present_with_placeholders_when_empty(self):
        context = build_memory_context([], {}, "", "")
        assert "Your recent actions" in context
        assert "no actions in the recall window" in context
        assert "no theses on record" in context
        assert "no lessons recorded yet" in context
        assert "no scorecard yet" in context

    def test_theses_flattened_to_single_lines(self):
        context = build_memory_context([], {"KHC": "line one\nline two"}, "", "")
        assert "KHC: line one line two" in context

    def test_lessons_and_scorecard_injected_verbatim(self):
        context = build_memory_context([], {}, "- [2026-07-16] rule X", "| momentum | 12 |")
        assert "- [2026-07-16] rule X" in context
        assert "| momentum | 12 |" in context


class TestVerifyLessonCitation:
    def test_no_citation_claimed_is_none(self):
        assert verify_lesson_citation("Strong bullish score, initiating a position.", LESSONS_MD) is None

    def test_genuine_citation_verified(self):
        reasoning = (
            "Skipping the add: per the lesson about unchanged momentum readings, "
            "waiting for confirmation from a second source before adding."
        )
        assert verify_lesson_citation(reasoning, LESSONS_MD) is True

    def test_fabricated_citation_flagged(self):
        reasoning = "Selling half per my lesson that tech stocks always drop on Fridays."
        assert verify_lesson_citation(reasoning, LESSONS_MD) is False

    def test_citation_with_empty_vault_is_fabricated_by_definition(self):
        assert verify_lesson_citation("Applying the lesson from last week.", "") is False

    def test_paraphrased_citation_still_verified(self):
        reasoning = (
            "Lesson applies here: momentum reading is unchanged since the last add, and no "
            "second source confirmation exists, so no add."
        )
        assert verify_lesson_citation(reasoning, LESSONS_MD) is True
