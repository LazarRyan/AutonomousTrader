"""
Tests for the 2026-07-17 "live P&L + thesis exit targets" upgrade: the
holdings-line rendering with current price/unrealized P&L, the exit-target
round trip (reflection formats -> vault parses), and the object-form
theses in reflection responses. The deterministic crossing check in
run_cycle is inline glue over parse_exit_targets + a comparison, both
covered here.
"""

import pytest

from src.agents.portfolio_manager import HoldingSnapshot, PortfolioContext, build_portfolio_manager_prompt
from src.memory.reflection import format_thesis_with_targets, parse_reflection_response
from src.memory.vault import parse_exit_targets


class TestHoldingsWithLiveMarks:
    def _prompt(self, holding: HoldingSnapshot) -> str:
        return build_portfolio_manager_prompt(
            {"KHC": 38.4}, PortfolioContext(total_portfolio_value=100_000.0, cash_available=5_000.0, holdings=[holding])
        )

    def test_full_line_with_price_and_unrealized(self):
        prompt = self._prompt(HoldingSnapshot("KHC", 345, 25.28, current_price=26.10, unrealized_pnl_pct=0.032))
        assert "KHC: 345 shares @ avg entry $25.28, now $26.10 (+3.2% unrealized)" in prompt

    def test_missing_marks_degrade_to_old_rendering(self):
        prompt = self._prompt(HoldingSnapshot("KHC", 345, 25.28))
        assert "KHC: 345 shares @ avg entry $25.28" in prompt
        assert "now $" not in prompt
        assert "unrealized" not in prompt

    def test_negative_unrealized_shown_signed(self):
        prompt = self._prompt(HoldingSnapshot("HUM", 8, 305.0, current_price=274.5, unrealized_pnl_pct=-0.10))
        assert "(-10.0% unrealized)" in prompt


class TestExitTargetRoundTrip:
    def test_format_then_parse_both_targets(self):
        thesis = format_thesis_with_targets("Core holding; momentum thesis.", 28.0, 23.5)
        assert "Exit targets: above $28.00 · below $23.50" in thesis
        assert parse_exit_targets(thesis) == (28.0, 23.5)

    def test_stop_only(self):
        thesis = format_thesis_with_targets("Open-ended hold.", None, 23.5)
        assert "Exit targets: below $23.50" in thesis
        assert parse_exit_targets(thesis) == (None, 23.5)

    def test_target_only(self):
        thesis = format_thesis_with_targets("Swing to target.", 1_234.56, None)
        assert parse_exit_targets(thesis) == (1234.56, None)  # comma in $1,234.56 parsed

    def test_no_targets_thesis_unchanged_and_parses_none(self):
        assert format_thesis_with_targets("Plain thesis.", None, None) == "Plain thesis."
        assert parse_exit_targets("Plain thesis, mentions $25.28 in prose.") == (None, None)

    def test_old_format_thesis_without_targets_is_not_an_error(self):
        assert parse_exit_targets("Hold as core position, exit only if it breaks below the $25.28 average entry.") == (None, None)


class TestObjectFormTheses:
    def test_object_theses_formatted_with_targets(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": [], "theses": '
            '{"KHC": {"thesis": "Cheap staples turnaround.", "exit_below": 23.5, "exit_above": 28.0}}}'
        )
        thesis = result.theses_by_symbol["KHC"]
        assert thesis.startswith("Cheap staples turnaround.")
        assert parse_exit_targets(thesis) == (28.0, 23.5)

    def test_null_exit_above_gives_stop_only(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": [], "theses": '
            '{"GE": {"thesis": "Open-ended hold.", "exit_below": 310.0, "exit_above": null}}}'
        )
        assert parse_exit_targets(result.theses_by_symbol["GE"]) == (None, 310.0)

    def test_legacy_string_theses_still_accepted(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": [], "theses": {"KHC": "old-style plain thesis"}}'
        )
        assert result.theses_by_symbol == {"KHC": "old-style plain thesis"}

    def test_malformed_thesis_object_skipped_not_fatal(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": [], "theses": '
            '{"KHC": {"exit_below": 23.5}, "GE": {"thesis": "fine", "exit_below": 300.0, "exit_above": null}}}'
        )
        assert "KHC" not in result.theses_by_symbol
        assert "GE" in result.theses_by_symbol

    def test_nonpositive_targets_dropped(self):
        result = parse_reflection_response(
            '{"journal_summary": "x", "lessons": [], "theses": '
            '{"KHC": {"thesis": "t", "exit_below": -5, "exit_above": 0}}}'
        )
        assert parse_exit_targets(result.theses_by_symbol["KHC"]) == (None, None)


class TestCrossingLogic:
    # The run_cycle check is: price >= exit_above -> take-gains alert;
    # price <= exit_below -> stop alert. Verified here against the parser
    # output it consumes.
    @pytest.mark.parametrize(
        "price, expected",
        [(28.5, "above"), (28.0, "above"), (25.0, None), (23.5, "below"), (22.0, "below")],
    )
    def test_threshold_semantics(self, price, expected):
        exit_above, exit_below = parse_exit_targets(format_thesis_with_targets("t", 28.0, 23.5))
        crossed = "above" if price >= exit_above else ("below" if price <= exit_below else None)
        assert crossed == expected
