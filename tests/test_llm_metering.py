import pytest

from src.llm_metering import PRICING_PER_MTOK, compute_cost_usd
from src.newsletter import OpsMetrics, build_ops_metrics


class TestComputeCost:
    def test_known_model_rates(self):
        input_rate, output_rate = PRICING_PER_MTOK["claude-sonnet-5"]
        cost, known = compute_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000)
        assert known is True
        assert cost == pytest.approx(input_rate + output_rate)

    def test_realistic_cycle_scale(self):
        # ~3k in / 500 out at sonnet rates: (3000*3 + 500*15) / 1e6
        cost, _ = compute_cost_usd("claude-sonnet-5", 3_000, 500)
        assert cost == pytest.approx(0.0165)

    def test_unknown_model_falls_back_and_flags(self):
        cost, known = compute_cost_usd("claude-future-9", 1_000, 0)
        assert known is False
        assert cost > 0

    def test_zero_tokens_zero_cost(self):
        assert compute_cost_usd("claude-sonnet-5", 0, 0) == (0.0, True)

    def test_negative_tokens_raise(self):
        with pytest.raises(ValueError):
            compute_cost_usd("claude-sonnet-5", -1, 0)


class TestBuildOpsMetrics:
    def _llm_rows(self):
        return [
            {"call_type": "news_sentiment", "input_tokens": 900, "output_tokens": 120, "cost_usd": 0.0045},
            {"call_type": "news_sentiment", "input_tokens": 800, "output_tokens": 100, "cost_usd": 0.0039},
            {"call_type": "portfolio_manager", "input_tokens": 4000, "output_tokens": 600, "cost_usd": 0.021},
        ]

    def test_totals_and_breakdown(self):
        metrics = build_ops_metrics(self._llm_rows(), [], [])
        assert metrics.llm_calls == 3
        assert metrics.input_tokens == 5700
        assert metrics.output_tokens == 820
        assert metrics.cost_usd == pytest.approx(0.0294)
        assert metrics.cost_by_type["news_sentiment"] == pytest.approx(0.0084)
        assert metrics.cost_by_type["portfolio_manager"] == pytest.approx(0.021)

    def test_guard_interventions_counted_and_labeled(self):
        audit_rows = [
            {"decision": "churn_suppressed"},
            {"decision": "churn_suppressed"},
            {"decision": "sector_cap_blocked"},
            {"decision": "auto_approved"},   # not a guard decision -- ignored
            {"decision": "cycle_skipped"},   # ignored
        ]
        metrics = build_ops_metrics([], audit_rows, [])
        assert metrics.guard_interventions == {
            "churn guard suppressions": 2,
            "sector cap blocks": 1,
        }

    def test_citation_check_rows_counted(self):
        audit_rows = [
            {"decision": "citation_verified"},
            {"decision": "citation_verified"},
            {"decision": "citation_unverified"},
            {"decision": "churn_suppressed"},
        ]
        metrics = build_ops_metrics([], audit_rows, [])
        assert metrics.citations_verified == 2
        assert metrics.citations_unverified == 1
        # citation rows are not guard interventions
        assert metrics.guard_interventions == {"churn guard suppressions": 1}

    def test_lesson_citation_detection(self):
        reasonings = [
            "Strong signal; per the lesson about unchanged signals, skipping the add elsewhere.",
            "Bullish score, initiating position.",
            "Applying Lesson from the IBM whipsaw: waiting for confirmation.",
        ]
        metrics = build_ops_metrics([], [], reasonings)
        assert metrics.trades_citing_lessons == 2
        assert metrics.total_trades == 3

    def test_empty_day(self):
        metrics = build_ops_metrics([], [], [])
        assert metrics.llm_calls == 0
        assert metrics.cost_usd == 0.0
        assert metrics.guard_interventions == {}
        assert metrics.total_trades == 0


class TestOpsMetricsRendering:
    def _metrics(self) -> OpsMetrics:
        return OpsMetrics(
            llm_calls=12, input_tokens=48_000, output_tokens=6_200, cost_usd=0.24,
            cost_by_type={"news_sentiment": 0.09, "portfolio_manager": 0.13, "reflection": 0.02},
            guard_interventions={"churn guard suppressions": 3},
            trades_citing_lessons=2, total_trades=5,
            citations_verified=1, citations_unverified=1,
        )

    def test_markdown_section(self):
        from datetime import date

        from src.newsletter import PerformanceSummary, render_newsletter

        text = render_newsletter(
            day=date(2026, 7, 17),
            performance=PerformanceSummary(None, None, None, None, None, 100_000.0),
            todays_trades=[], journal_summary="Quiet.", lessons=[],
            scorecard_markdown="", regime_line=None, ops_metrics=self._metrics(),
        )
        assert "## Under the hood" in text
        assert "**$0.24** across 12 call(s)" in text
        assert "48,000 in / 6,200 out" in text
        assert "3 churn guard suppressions" in text
        assert "2 of 5 proposal(s) cited a learned lesson" in text
        assert "1 verified against the vault, 1 NOT matching any vault lesson" in text

    def test_html_section_and_omitted_when_absent(self):
        from datetime import date

        from src.newsletter import PerformanceSummary, render_newsletter_html

        kwargs = dict(
            day=date(2026, 7, 17),
            performance=PerformanceSummary(None, None, None, None, None, 100_000.0),
            todays_trades=[], journal_summary="Quiet.", lessons=[],
            scorecard_markdown="", regime_line=None,
        )
        html = render_newsletter_html(**kwargs, ops_metrics=self._metrics())
        assert "Under the hood" in html
        assert "$0.24" in html
        assert "churn guard suppressions" in html
        absent = render_newsletter_html(**kwargs)
        assert "Under the hood" not in absent