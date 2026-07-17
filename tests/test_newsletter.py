from datetime import date

import pytest

from src.newsletter import (
    EquitySnapshot,
    PerformanceSummary,
    TradeLine,
    _inline_md_to_html,
    _markdown_table_to_html,
    compute_performance,
    render_newsletter,
    render_newsletter_html,
)


def snapshot(day: int, equity: float, spy: float | None) -> EquitySnapshot:
    return EquitySnapshot(snapshot_date=date(2026, 7, day), equity=equity, spy_close=spy)


class TestComputePerformance:
    def test_day_and_inception_returns(self):
        perf = compute_performance([snapshot(10, 100_000, 500.0), snapshot(15, 102_000, 505.0), snapshot(16, 101_000, 510.0)])
        assert perf.day_return_pct == pytest.approx(-1_000 / 102_000)
        assert perf.since_inception_return_pct == pytest.approx(0.01)
        assert perf.spy_day_return_pct == pytest.approx(5.0 / 505.0)
        assert perf.spy_since_inception_return_pct == pytest.approx(0.02)
        assert perf.inception_date == date(2026, 7, 10)

    def test_first_snapshot_ever_has_no_returns(self):
        perf = compute_performance([snapshot(16, 100_000, 500.0)])
        assert perf.day_return_pct is None
        assert perf.since_inception_return_pct is None
        assert perf.inception_date is None
        assert perf.latest_equity == 100_000

    def test_missing_spy_closes_degrade_to_none_not_zero(self):
        perf = compute_performance([snapshot(15, 100_000, None), snapshot(16, 101_000, 510.0)])
        assert perf.day_return_pct == pytest.approx(0.01)
        assert perf.spy_day_return_pct is None
        assert perf.spy_since_inception_return_pct is None

    def test_unordered_input_is_sorted(self):
        perf = compute_performance([snapshot(16, 101_000, None), snapshot(10, 100_000, None)])
        assert perf.since_inception_return_pct == pytest.approx(0.01)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            compute_performance([])


class TestRenderNewsletter:
    def _performance(self) -> PerformanceSummary:
        return PerformanceSummary(
            day_return_pct=0.004, since_inception_return_pct=0.021,
            spy_day_return_pct=0.002, spy_since_inception_return_pct=0.030,
            inception_date=date(2026, 7, 10), latest_equity=102_100.0,
        )

    def test_full_newsletter_sections(self):
        text = render_newsletter(
            day=date(2026, 7, 16),
            performance=self._performance(),
            todays_trades=[TradeLine("KHC", "buy", 20, "executed", "strong signal")],
            journal_summary="Two adds, one trim.",
            lessons=["Do not add on unchanged signals."],
            scorecard_markdown="# Signal Source Scorecard\n\n| momentum | 12 |",
            regime_line="**risk_on** — calm",
        )
        assert "Daily Letter, 2026-07-16" in text
        assert "| Today | +0.40% | +0.20% |" in text
        assert "| Since 2026-07-10 | +2.10% | +3.00% |" in text
        assert "**KHC BUY x20** [executed] — strong signal" in text
        assert "Two adds, one trim." in text
        assert "## Lessons learned" in text
        assert "**New tonight:**" in text
        assert "Do not add on unchanged signals." in text
        assert "**risk_on** — calm" in text
        assert "| momentum | 12 |" in text
        assert "# Signal Source Scorecard" not in text  # scorecard H1 stripped, newsletter keeps one title
        assert "Not financial advice" in text

    def test_quiet_day_and_missing_benchmark(self):
        performance = PerformanceSummary(
            day_return_pct=None, since_inception_return_pct=None,
            spy_day_return_pct=None, spy_since_inception_return_pct=None,
            inception_date=None, latest_equity=100_000.0,
        )
        text = render_newsletter(
            day=date(2026, 7, 16), performance=performance, todays_trades=[],
            journal_summary="Quiet.", lessons=[], scorecard_markdown="", regime_line=None,
        )
        assert "No trades today." in text
        assert "| Today | n/a | n/a |" in text
        assert "Since" not in text.split("## The day in trades")[0].split("| Today")[1]
        # Lessons section is ALWAYS present, with the honest placeholder
        # before the first position ever closes.
        assert "## Lessons learned" in text
        assert "No realized-outcome lessons yet" in text

    def test_standing_lessons_rendered_and_deduped_from_tonights(self):
        text = render_newsletter(
            day=date(2026, 7, 16), performance=self._performance(), todays_trades=[],
            journal_summary="Quiet.", lessons=["fresh rule"],
            scorecard_markdown="", regime_line=None,
            cumulative_lessons=["fresh rule", "[2026-07-10] older standing rule"],
        )
        assert "**New tonight:**" in text
        assert "**Standing lessons (in effect every cycle):**" in text
        assert "older standing rule" in text
        assert text.count("fresh rule") == 1  # not repeated in the standing list
        assert "No realized-outcome lessons yet" not in text


class TestRenderNewsletterHtml:
    def _performance(self) -> PerformanceSummary:
        return PerformanceSummary(
            day_return_pct=0.004, since_inception_return_pct=-0.021,
            spy_day_return_pct=0.002, spy_since_inception_return_pct=0.030,
            inception_date=date(2026, 7, 10), latest_equity=102_100.0,
        )

    def _render(self, **overrides) -> str:
        kwargs = dict(
            day=date(2026, 7, 16),
            performance=self._performance(),
            todays_trades=[TradeLine("KHC", "buy", 20, "executed", "strong signal")],
            journal_summary="Two adds, one trim.",
            lessons=["Do not add on unchanged signals."],
            scorecard_markdown="# Signal Source Scorecard\n\n| Source | Hits |\n|---|---|\n| momentum | 12 |\n",
            regime_line="**risk_on** — calm",
        )
        kwargs.update(overrides)
        return render_newsletter_html(**kwargs)

    def test_no_raw_markdown_artifacts_in_html(self):
        html = self._render()
        assert "**" not in html          # the exact Gmail complaint: literal asterisks
        assert "| Today |" not in html   # and literal pipe tables
        assert "<strong>risk_on</strong>" in html

    def test_sections_and_values_present(self):
        html = self._render()
        assert "$102,100.00" in html
        assert "+0.40%" in html
        assert "Since 2026-07-10" in html
        assert "KHC BUY &times;20" in html  # entity, not raw U+00D7 -- see render comment
        assert "strong signal" in html
        assert "Two adds, one trim." in html
        assert "Do not add on unchanged signals." in html
        assert "Not financial advice" in html

    def test_negative_and_missing_returns_styled_not_faked(self):
        html = self._render()
        assert "-2.10%" in html  # negative since-inception renders with sign
        quiet = self._render(
            performance=PerformanceSummary(None, None, None, None, None, 100_000.0),
            todays_trades=[], lessons=[], scorecard_markdown="", regime_line=None,
        )
        assert ">n/a</td>" in quiet
        assert "No trades today." in quiet
        assert "Signal scorecard" not in quiet  # empty scorecard section omitted entirely
        assert "Lessons learned" in quiet  # but lessons section always shows
        assert "No realized-outcome lessons yet" in quiet

    def test_html_standing_lessons_section(self):
        html = self._render(cumulative_lessons=["Do not add on unchanged signals.", "[2026-07-10] older rule"])
        assert "New tonight" in html
        assert "Standing lessons (in effect every cycle)" in html
        assert "older rule" in html
        assert html.count("Do not add on unchanged signals.") == 1  # deduped against tonight's

    def test_reasoning_html_escaped(self):
        html = self._render(todays_trades=[TradeLine("KHC", "buy", 20, "executed", "P/E <12 & cheap")])
        assert "P/E &lt;12 &amp; cheap" in html
        assert "P/E <12" not in html

    def test_scorecard_table_converted(self):
        table = _markdown_table_to_html("| Source | Hits |\n|---|---|\n| momentum | 12 |")
        assert "<th" in table and "momentum" in table and "|" not in table.replace("</", "").split(">")[-1]
        assert _markdown_table_to_html("") == ""

    def test_inline_bold_conversion_and_escaping(self):
        assert _inline_md_to_html("**bold** & <plain>") == "<strong>bold</strong> &amp; &lt;plain&gt;"
