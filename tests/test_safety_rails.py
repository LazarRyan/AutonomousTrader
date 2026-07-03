from datetime import date

import pytest

from src.risk.safety_rails import SafetyConfig, SafetyState, evaluate_trade


def test_clean_state_allows_normal_trade():
    state = SafetyState()
    decision = evaluate_trade(trade_value=1_000, total_portfolio_value=100_000, state=state)
    assert decision.allowed is True
    assert decision.reasons == []


def test_kill_switch_blocks_everything():
    state = SafetyState(kill_switch_engaged=True)
    decision = evaluate_trade(trade_value=100, total_portfolio_value=100_000, state=state)
    assert decision.allowed is False
    assert any("kill_switch" in r for r in decision.reasons)


def test_max_position_size_blocks_oversized_trade():
    state = SafetyState()
    config = SafetyConfig(max_position_pct=0.05)
    # 6% of portfolio -- exceeds 5% cap
    decision = evaluate_trade(
        trade_value=6_000, total_portfolio_value=100_000, state=state, config=config
    )
    assert decision.allowed is False
    assert any("max_position_size" in r for r in decision.reasons)


def test_daily_loss_limit_auto_halts_and_blocks():
    state = SafetyState()
    config = SafetyConfig(max_daily_loss_pct=0.03)
    state.record_pnl(daily_pnl_pct=-0.035, weekly_pnl_pct=-0.01, config=config)
    assert state.daily_halted is True

    decision = evaluate_trade(trade_value=100, total_portfolio_value=100_000, state=state, config=config)
    assert decision.allowed is False
    assert any("daily_loss_limit" in r for r in decision.reasons)


def test_daily_halt_clears_on_daily_reset():
    state = SafetyState()
    config = SafetyConfig(max_daily_loss_pct=0.03)
    state.record_pnl(daily_pnl_pct=-0.04, weekly_pnl_pct=-0.01, config=config)
    assert state.daily_halted is True

    state.reset_daily(today=date(2026, 7, 4))
    assert state.daily_halted is False
    assert state.daily_pnl_pct == 0.0

    decision = evaluate_trade(trade_value=100, total_portfolio_value=100_000, state=state, config=config)
    assert decision.allowed is True


def test_weekly_loss_limit_halts_and_does_not_auto_clear_on_daily_reset():
    state = SafetyState()
    config = SafetyConfig(max_weekly_loss_pct=0.08)
    state.record_pnl(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.09, config=config)
    assert state.weekly_halted is True

    # A daily reset must NOT clear a weekly halt.
    state.reset_daily(today=date(2026, 7, 4))
    assert state.weekly_halted is True

    decision = evaluate_trade(trade_value=100, total_portfolio_value=100_000, state=state, config=config)
    assert decision.allowed is False
    assert any("weekly_loss_limit" in r for r in decision.reasons)


def test_weekly_halt_requires_explicit_manual_reset():
    state = SafetyState()
    config = SafetyConfig(max_weekly_loss_pct=0.08)
    state.record_pnl(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.09, config=config)
    assert state.weekly_halted is True

    state.reset_weekly(today=date(2026, 7, 6))
    assert state.weekly_halted is False

    decision = evaluate_trade(trade_value=100, total_portfolio_value=100_000, state=state, config=config)
    assert decision.allowed is True


def test_all_applicable_reasons_are_reported_not_just_first():
    state = SafetyState(kill_switch_engaged=True)
    config = SafetyConfig(max_position_pct=0.05, max_daily_loss_pct=0.03)
    state.record_pnl(daily_pnl_pct=-0.05, weekly_pnl_pct=-0.01, config=config)

    decision = evaluate_trade(
        trade_value=10_000, total_portfolio_value=100_000, state=state, config=config
    )
    assert decision.allowed is False
    reason_types = decision.reasoning
    assert "kill_switch" in reason_types
    assert "daily_loss_limit" in reason_types
    assert "max_position_size" in reason_types


def test_loss_limit_is_a_one_way_latch_within_period():
    """A partial recovery mid-day should not silently un-halt trading."""
    state = SafetyState()
    config = SafetyConfig(max_daily_loss_pct=0.03)
    state.record_pnl(daily_pnl_pct=-0.04, weekly_pnl_pct=-0.01, config=config)
    assert state.daily_halted is True

    # P&L improves but is still negative -- halt must remain engaged.
    state.record_pnl(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.01, config=config)
    assert state.daily_halted is True


def test_default_config_leaves_a_real_approval_band_above_scorer_threshold():
    # Regression test for a real bug found on a live dry run: this module's
    # default max_position_pct used to be identical to risk/scorer.py's
    # hard_override_position_pct (both 0.05), so ANY trade routed to the
    # approval queue for exceeding 5% position size was guaranteed to be
    # blocked here regardless of what a human approved -- three real trades
    # (ABBV 16.57%, AEP 5.90%, ALGN 6.80%) were all approved via the CLI
    # watcher, then all blocked immediately after. The default is now 0.15,
    # specifically to leave a 5%-15% band where an approval can actually
    # lead to execution.
    state = SafetyState()
    # 8% of portfolio -- above risk/scorer.py's 5% approval trigger, but
    # within this module's default 15% hard cap. Uses the default config
    # (no override) since that's what main.py and review_approvals.py
    # actually construct in production.
    decision = evaluate_trade(trade_value=8_000, total_portfolio_value=100_000, state=state)
    assert decision.allowed is True


def test_default_config_still_hard_blocks_far_oversized_trade():
    state = SafetyState()
    # 20% of portfolio -- above even the widened 15% default hard cap.
    decision = evaluate_trade(trade_value=20_000, total_portfolio_value=100_000, state=state)
    assert decision.allowed is False
    assert any("max_position_size" in r for r in decision.reasons)


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        SafetyConfig(max_position_pct=0.0)
    with pytest.raises(ValueError):
        SafetyConfig(max_daily_loss_pct=1.5)


def test_rejects_nonpositive_portfolio_value():
    state = SafetyState()
    with pytest.raises(ValueError):
        evaluate_trade(trade_value=100, total_portfolio_value=0, state=state)
