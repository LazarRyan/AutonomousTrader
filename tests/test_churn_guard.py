from datetime import datetime, timedelta, timezone

import pytest

from src.risk.churn_guard import ChurnGuardConfig, PastExecution, evaluate_churn

NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)


def make_execution(hours_ago: float, **overrides) -> PastExecution:
    defaults = dict(
        symbol="KHC",
        side="buy",
        status="executed",
        blended_signal_score=38.0,
        executed_at=NOW - timedelta(hours=hours_ago),
    )
    defaults.update(overrides)
    return PastExecution(**defaults)


class TestCooldown:
    def test_repeat_buy_within_cooldown_same_score_is_suppressed(self):
        decision = evaluate_churn("KHC", "buy", 39.0, [make_execution(hours_ago=4)], now=NOW)
        assert decision.allowed is False
        assert "same information re-observed" in decision.reason

    def test_material_score_move_is_new_information(self):
        decision = evaluate_churn("KHC", "buy", 60.0, [make_execution(hours_ago=4, blended_signal_score=38.0)], now=NOW)
        assert decision.allowed is True
        assert "new information" in decision.reason

    def test_outside_cooldown_is_allowed(self):
        decision = evaluate_churn("KHC", "buy", 39.0, [make_execution(hours_ago=30)], now=NOW)
        assert decision.allowed is True
        assert "outside cooldown" in decision.reason

    def test_missing_scores_cannot_claim_new_information(self):
        decision = evaluate_churn("KHC", "buy", None, [make_execution(hours_ago=4, blended_signal_score=None)], now=NOW)
        assert decision.allowed is False

    def test_repeat_sell_also_cooled_down(self):
        # The NKE incident: identical "sell 50" trims on consecutive cycles
        # with the score unchanged.
        decision = evaluate_churn("NKE", "sell", -2.5, [make_execution(hours_ago=4, symbol="NKE", side="sell", blended_signal_score=-2.4)], now=NOW)
        assert decision.allowed is False


class TestWindowCap:
    def test_two_recent_buys_cap_the_third_regardless_of_score(self):
        history = [make_execution(hours_ago=30, blended_signal_score=38.0), make_execution(hours_ago=54, blended_signal_score=25.0)]
        decision = evaluate_churn("KHC", "buy", 90.0, history, now=NOW)
        assert decision.allowed is False
        assert "anti-concentration cap" in decision.reason

    def test_old_buys_outside_window_do_not_count(self):
        history = [make_execution(hours_ago=24 * 6), make_execution(hours_ago=24 * 8)]
        decision = evaluate_churn("KHC", "buy", 39.0, history, now=NOW)
        assert decision.allowed is True

    def test_sells_are_never_capped(self):
        # Exits must never be structurally hard -- see module docstring.
        history = [
            make_execution(hours_ago=30, side="sell"),
            make_execution(hours_ago=54, side="sell"),
            make_execution(hours_ago=70, side="sell", blended_signal_score=-40.0),
        ]
        decision = evaluate_churn("KHC", "sell", -41.0, history, now=NOW)
        assert decision.allowed is True


class TestFiltering:
    def test_no_prior_executions_is_allowed(self):
        decision = evaluate_churn("KHC", "buy", 39.0, [], now=NOW)
        assert decision.allowed is True
        assert "no prior executed" in decision.reason

    def test_other_symbols_and_sides_ignored(self):
        history = [make_execution(hours_ago=1, symbol="GE"), make_execution(hours_ago=1, side="sell")]
        decision = evaluate_churn("KHC", "buy", 39.0, history, now=NOW)
        assert decision.allowed is True

    def test_blocked_and_rejected_statuses_do_not_start_cooldowns(self):
        history = [make_execution(hours_ago=1, status="blocked"), make_execution(hours_ago=2, status="rejected")]
        decision = evaluate_churn("KHC", "buy", 39.0, history, now=NOW)
        assert decision.allowed is True

    def test_symbol_match_is_case_insensitive(self):
        decision = evaluate_churn("khc", "buy", 39.0, [make_execution(hours_ago=4)], now=NOW)
        assert decision.allowed is False


class TestConfigValidation:
    def test_bad_config_values_raise(self):
        with pytest.raises(ValueError):
            ChurnGuardConfig(cooldown_hours=0)
        with pytest.raises(ValueError):
            ChurnGuardConfig(max_same_side_executions_per_window=0)
        with pytest.raises(ValueError):
            ChurnGuardConfig(window_days=0)
        with pytest.raises(ValueError):
            ChurnGuardConfig(new_information_score_delta=-1)
