from datetime import date, datetime, timezone

import pytest

from src.memory.reflection import (
    MAX_LESSONS_PER_NIGHT,
    ClosedLot,
    ExecutionRecord,
    OpenPositionSummary,
    build_reflection_prompt,
    match_closed_lots,
    parse_reflection_response,
)


def make_execution(symbol="KHC", side="buy", quantity=10.0, fill_price=25.0, day=14, reasoning="entry logic") -> ExecutionRecord:
    return ExecutionRecord(
        symbol=symbol,
        side=side,
        quantity=quantity,
        fill_price=fill_price,
        executed_at=datetime(2026, 7, day, 15, 0, tzinfo=timezone.utc),
        reasoning=reasoning,
        blended_signal_score=30.0,
    )


class TestMatchClosedLots:
    def test_simple_round_trip(self):
        lots = match_closed_lots([
            make_execution(side="buy", quantity=10, fill_price=25.0, day=10, reasoning="cheap"),
            make_execution(side="sell", quantity=10, fill_price=27.5, day=14, reasoning="target hit"),
        ])
        assert len(lots) == 1
        lot = lots[0]
        assert lot.realized_pnl == pytest.approx(25.0)
        assert lot.realized_pnl_pct == pytest.approx(0.10)
        assert lot.entry_reasoning == "cheap"
        assert lot.exit_reasoning == "target hit"

    def test_fifo_partial_matching_across_lots(self):
        lots = match_closed_lots([
            make_execution(side="buy", quantity=10, fill_price=20.0, day=8),
            make_execution(side="buy", quantity=10, fill_price=30.0, day=10),
            make_execution(side="sell", quantity=15, fill_price=25.0, day=14),
        ])
        assert len(lots) == 2
        assert lots[0].quantity == 10 and lots[0].entry_price == 20.0   # first lot fully closed
        assert lots[1].quantity == 5 and lots[1].entry_price == 30.0    # second lot half closed

    def test_sell_without_inventory_is_skipped(self):
        lots = match_closed_lots([make_execution(side="sell", quantity=10, day=14)])
        assert lots == []

    def test_missing_fill_price_is_skipped(self):
        lots = match_closed_lots([
            make_execution(side="buy", quantity=10, fill_price=None, day=10),
            make_execution(side="sell", quantity=10, fill_price=25.0, day=14),
        ])
        assert lots == []  # no honest entry price -> no lot

    def test_symbols_matched_independently(self):
        lots = match_closed_lots([
            make_execution(symbol="KHC", side="buy", quantity=10, fill_price=20.0, day=8),
            make_execution(symbol="GE", side="sell", quantity=10, fill_price=100.0, day=14),
        ])
        assert lots == []  # GE sell can't consume KHC inventory


class TestBuildReflectionPrompt:
    def test_all_sections_present_with_placeholders(self):
        prompt = build_reflection_prompt(date(2026, 7, 16), [], [], [], "")
        assert "no executions today" in prompt
        assert "no lots closed recently" in prompt
        assert "no open positions" in prompt
        assert "(none yet)" in prompt

    def test_closed_lot_carries_both_reasonings_and_pnl(self):
        lot = ClosedLot(
            symbol="IBM", quantity=15, entry_price=280.0, exit_price=260.0,
            entry_at=datetime(2026, 7, 8, tzinfo=timezone.utc), exit_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            entry_reasoning="strong bullish score", exit_reasoning="signal turned bearish",
        )
        prompt = build_reflection_prompt(date(2026, 7, 16), [], [lot], [], "- [old] lesson")
        assert "OPENED because: strong bullish score" in prompt
        assert "CLOSED because: signal turned bearish" in prompt
        assert "-7.14%" in prompt
        assert "- [old] lesson" in prompt

    def test_open_position_unrealized_rendered(self):
        position = OpenPositionSummary(symbol="SPGI", quantity=22, avg_entry_price=500.0, current_price=550.0, unrealized_pnl_pct=0.10)
        prompt = build_reflection_prompt(date(2026, 7, 16), [], [], [position], "")
        assert "+10.00% unrealized" in prompt


class TestParseReflectionResponse:
    def test_valid_response(self):
        result = parse_reflection_response(
            '{"journal_summary": "Quiet day.", "lessons": ["Rule 1"], "theses": {"khc": "Hold until X."}}'
        )
        assert result.journal_summary == "Quiet day."
        assert result.lessons == ["Rule 1"]
        assert result.theses_by_symbol == {"KHC": "Hold until X."}

    def test_fenced_and_prefixed_responses_tolerated(self):
        assert parse_reflection_response('```json\n{"journal_summary": "x", "lessons": [], "theses": {}}\n```').journal_summary == "x"
        assert parse_reflection_response('Here you go: {"journal_summary": "x", "lessons": [], "theses": {}}').journal_summary == "x"

    def test_lesson_cap_enforced_even_if_model_ignores_it(self):
        lessons = [f"rule {i}" for i in range(MAX_LESSONS_PER_NIGHT + 5)]
        payload = '{"journal_summary": "x", "lessons": ' + str(lessons).replace("'", '"') + ', "theses": {}}'
        assert len(parse_reflection_response(payload).lessons) == MAX_LESSONS_PER_NIGHT

    def test_missing_summary_raises(self):
        with pytest.raises(ValueError):
            parse_reflection_response('{"lessons": [], "theses": {}}')

    def test_non_object_raises(self):
        with pytest.raises(ValueError):
            parse_reflection_response("[1, 2]")

    def test_malformed_entries_dropped_not_fatal(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": ["ok", 42, "  "], "theses": {"KHC": "ok", "GE": 7}}'
        )
        assert result.lessons == ["ok"]
        assert result.theses_by_symbol == {"KHC": "ok"}
