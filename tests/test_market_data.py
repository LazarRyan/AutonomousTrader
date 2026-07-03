import pytest

from src.risk.market_data import LiquidityConfig, compute_liquidity_penalty, compute_volatility


class TestComputeVolatility:
    def test_flat_prices_have_zero_volatility(self):
        closes = [100.0] * 30
        assert compute_volatility(closes) == pytest.approx(0.0)

    def test_choppy_prices_have_higher_volatility_than_smooth_prices(self):
        smooth = [100.0 + i * 0.1 for i in range(30)]
        choppy = [100.0 + (10 if i % 2 == 0 else -10) for i in range(30)]
        assert compute_volatility(choppy) > compute_volatility(smooth)

    def test_raises_on_insufficient_data(self):
        with pytest.raises(ValueError):
            compute_volatility([100.0, 101.0])

    def test_raises_on_nonpositive_close(self):
        with pytest.raises(ValueError):
            compute_volatility([100.0, 0.0, 101.0, 102.0])

    def test_known_return_series_matches_manual_calculation(self):
        # closes -> returns: +10%, -10% (approx), +10% ...
        closes = [100.0, 110.0, 99.0, 108.9]
        returns = [(110 - 100) / 100, (99 - 110) / 110, (108.9 - 99) / 99]
        import statistics

        expected = statistics.stdev(returns)
        assert compute_volatility(closes) == pytest.approx(expected)


class TestComputeLiquidityPenalty:
    def test_high_dollar_volume_has_zero_penalty(self):
        closes = [100.0] * 10
        volumes = [1_000_000] * 10  # $100M/day -- well above default full-liquidity threshold
        assert compute_liquidity_penalty(closes, volumes) == 0.0

    def test_zero_volume_has_max_penalty(self):
        closes = [100.0] * 10
        volumes = [0] * 10
        assert compute_liquidity_penalty(closes, volumes) == 100.0

    def test_half_of_threshold_gives_roughly_half_penalty(self):
        config = LiquidityConfig(full_liquidity_dollar_volume=10_000_000.0)
        closes = [100.0] * 10
        volumes = [50_000] * 10  # $5M/day -- half the $10M threshold
        penalty = compute_liquidity_penalty(closes, volumes, config=config)
        assert penalty == pytest.approx(50.0)

    def test_penalty_never_exceeds_100_or_goes_negative(self):
        config = LiquidityConfig(full_liquidity_dollar_volume=1_000.0)
        closes = [100.0] * 5
        volumes = [1_000_000] * 5  # far above threshold
        assert compute_liquidity_penalty(closes, volumes, config=config) == 0.0

    def test_raises_on_mismatched_lengths(self):
        with pytest.raises(ValueError):
            compute_liquidity_penalty([100.0, 101.0], [1000.0])

    def test_raises_on_empty_input(self):
        with pytest.raises(ValueError):
            compute_liquidity_penalty([], [])

    def test_raises_on_negative_volume(self):
        with pytest.raises(ValueError):
            compute_liquidity_penalty([100.0], [-5.0])

    def test_raises_on_nonpositive_close(self):
        with pytest.raises(ValueError):
            compute_liquidity_penalty([0.0], [1000.0])

    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            LiquidityConfig(full_liquidity_dollar_volume=0.0)
