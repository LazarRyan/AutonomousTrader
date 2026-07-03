import pytest

from src.signals.momentum import MomentumConfig, compute_momentum_score


def uptrend(n: int = 60, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def downtrend(n: int = 60, start: float = 200.0, step: float = 1.0) -> list[float]:
    return [start - i * step for i in range(n)]


def flat(n: int = 60, price: float = 100.0) -> list[float]:
    return [price for _ in range(n)]


def test_uptrend_is_strongly_bullish():
    result = compute_momentum_score("AAPL", uptrend())
    assert result.momentum_score > 50
    assert result.trend_component > 0
    assert result.rsi_component > 0
    assert result.roc_component > 0


def test_downtrend_is_strongly_bearish():
    result = compute_momentum_score("AAPL", downtrend())
    assert result.momentum_score < -50
    assert result.trend_component < 0
    assert result.rsi_component < 0
    assert result.roc_component < 0


def test_flat_price_series_is_neutral():
    result = compute_momentum_score("AAPL", flat())
    assert result.momentum_score == pytest.approx(0.0)
    assert result.trend_component == pytest.approx(0.0)
    assert result.rsi_component == pytest.approx(0.0)  # RSI defined as 50 when flat
    assert result.roc_component == pytest.approx(0.0)


def test_rsi_is_100_when_every_change_is_a_gain():
    result = compute_momentum_score("AAPL", uptrend())
    assert result.rsi == pytest.approx(100.0)


def test_rsi_is_0_when_every_change_is_a_loss():
    result = compute_momentum_score("AAPL", downtrend())
    assert result.rsi == pytest.approx(0.0)


def test_insufficient_data_raises():
    with pytest.raises(ValueError):
        compute_momentum_score("AAPL", uptrend(n=30))  # needs 50 for default SMA long


def test_rejects_nonpositive_closes():
    closes = uptrend(n=60)
    closes[5] = 0.0
    with pytest.raises(ValueError):
        compute_momentum_score("AAPL", closes)


def test_trend_and_roc_are_clipped_not_unbounded():
    # A dramatic move should still cap out at +/-100 on each component,
    # never overshoot past the scale.
    closes = uptrend(n=60, start=100.0, step=20.0)  # huge daily jumps
    result = compute_momentum_score("AAPL", closes)
    assert result.trend_component == pytest.approx(100.0)
    assert result.roc_component == pytest.approx(100.0)
    assert result.momentum_score <= 100.0


def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        MomentumConfig(trend_weight=0.5, rsi_weight=0.5, roc_weight=0.5)


def test_short_window_must_be_less_than_long_window():
    with pytest.raises(ValueError):
        MomentumConfig(sma_short_window=50, sma_long_window=20)


def test_min_required_closes_reflects_largest_window():
    config = MomentumConfig(sma_short_window=20, sma_long_window=50, rsi_period=14, roc_period=10)
    assert config.min_required_closes == 50

    config2 = MomentumConfig(sma_short_window=5, sma_long_window=10, rsi_period=20, roc_period=10)
    assert config2.min_required_closes == 21  # rsi_period + 1


def test_reasoning_string_reflects_direction():
    bullish = compute_momentum_score("AAPL", uptrend())
    bearish = compute_momentum_score("AAPL", downtrend())
    neutral = compute_momentum_score("AAPL", flat())
    assert "bullish" in bullish.reasoning
    assert "bearish" in bearish.reasoning
    assert "neutral" in neutral.reasoning
