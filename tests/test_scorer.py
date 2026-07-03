import pytest

from src.risk.scorer import RiskScorerConfig, TradeRiskInputs, score_trade


def make_inputs(**overrides) -> TradeRiskInputs:
    defaults = dict(
        symbol="AAPL",
        trade_value=1_000.0,
        total_portfolio_value=100_000.0,
        asset_30d_volatility=0.02,
        benchmark_30d_volatility=0.02,
        liquidity_penalty=0.0,
    )
    defaults.update(overrides)
    return TradeRiskInputs(**defaults)


def test_low_risk_trade_is_auto_eligible():
    result = score_trade(make_inputs())
    assert result.needs_approval is False
    assert result.hard_override_triggered is False
    assert result.composite_score < 70


def test_hard_override_above_5pct_position_forces_approval_even_if_score_low():
    # 6% of portfolio, but otherwise totally benign (low vol, no liquidity penalty)
    result = score_trade(make_inputs(trade_value=6_000.0))
    assert result.hard_override_triggered is True
    assert result.needs_approval is True


def test_composite_score_above_threshold_forces_approval_even_under_5pct():
    # Small position (1%) but very volatile and illiquid.
    #
    # NOTE ON DEFAULT WEIGHTS: with the default weights (0.5/0.3/0.2) and a
    # position capped at the 5% hard-override boundary, the maximum possible
    # composite score without tripping the hard override is
    # 0.5*5 + 0.3*100 + 0.2*100 = 52.5 -- i.e. the default 70-point threshold
    # can, in practice, only ever be reached together with the hard override,
    # never on its own. That may be fine (the hard override already forces
    # approval in that region) but it means the composite-score threshold is
    # only independently meaningful at a lower value, or with reweighted
    # inputs. Flagging here rather than silently asserting around it -- worth
    # revisiting once real backtested data informs the weights/threshold.
    config = RiskScorerConfig(approval_threshold=45.0)
    result = score_trade(
        make_inputs(
            trade_value=1_000.0,
            asset_30d_volatility=0.10,
            benchmark_30d_volatility=0.02,  # 5x benchmark vol, capped at 3x
            liquidity_penalty=90.0,
        ),
        config=config,
    )
    assert result.hard_override_triggered is False
    assert result.composite_score >= config.approval_threshold
    assert result.needs_approval is True


def test_exactly_5pct_position_does_not_trigger_hard_override():
    # Hard override is "> 5%", not ">= 5%"
    result = score_trade(make_inputs(trade_value=5_000.0))
    assert result.hard_override_triggered is False


def test_volatility_ratio_is_capped():
    config = RiskScorerConfig(volatility_ratio_cap=3.0)
    normal = score_trade(
        make_inputs(asset_30d_volatility=0.06, benchmark_30d_volatility=0.02),  # 3x
        config=config,
    )
    extreme = score_trade(
        make_inputs(asset_30d_volatility=0.60, benchmark_30d_volatility=0.02),  # 30x, capped to 3x
        config=config,
    )
    assert normal.volatility_component == pytest.approx(extreme.volatility_component)


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        RiskScorerConfig(size_weight=0.5, volatility_weight=0.5, liquidity_weight=0.5)


def test_rejects_nonpositive_portfolio_value():
    with pytest.raises(ValueError):
        score_trade(make_inputs(total_portfolio_value=0))


def test_rejects_negative_trade_value():
    with pytest.raises(ValueError):
        score_trade(make_inputs(trade_value=-100))


def test_rejects_out_of_range_liquidity_penalty():
    with pytest.raises(ValueError):
        score_trade(make_inputs(liquidity_penalty=150))


def test_reasoning_string_is_populated():
    result = score_trade(make_inputs(trade_value=6_000.0))
    assert "hard override" in result.reasoning
