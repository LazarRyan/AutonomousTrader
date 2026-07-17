"""
Tests for the 2026-07-16 full-market expansion's pure pieces: the clean-
ticker filter behind fetch_tradable_universe (src/universe.py) and the
hard liquidity floor (src/risk/market_data.py). The Alpaca asset-master
fetch itself is thin untested glue, same as every network wrapper here.
"""

import pytest

from src.risk.market_data import LiquidityFloorConfig, passes_liquidity_floor
from src.universe import is_clean_common_stock_symbol


class TestCleanSymbolFilter:
    def test_plain_tickers_pass(self):
        for symbol in ("A", "GE", "KHC", "GOOGL", "BRKB"):
            assert is_clean_common_stock_symbol(symbol) is True

    def test_units_warrants_preferreds_and_junk_rejected(self):
        for symbol in ("ABC.U", "ABC.WS", "ABC-A", "BRK/B", "ABC1", "", "TOOLONGG"):
            assert is_clean_common_stock_symbol(symbol) is False


class TestLiquidityFloor:
    def _series(self, price: float, volume: float, n: int = 30):
        return [price] * n, [volume] * n

    def test_liquid_mid_price_name_passes(self):
        closes, volumes = self._series(price=25.0, volume=1_000_000)  # $25M/day
        ok, reason = passes_liquidity_floor(closes, volumes)
        assert ok is True
        assert "clear the floor" in reason

    def test_sub_three_dollar_name_blocked_on_price(self):
        closes, volumes = self._series(price=2.50, volume=10_000_000)  # liquid but cheap
        ok, reason = passes_liquidity_floor(closes, volumes)
        assert ok is False
        assert "below the $3.00 floor" in reason

    def test_thin_name_blocked_on_dollar_volume(self):
        closes, volumes = self._series(price=50.0, volume=20_000)  # $1M/day
        ok, reason = passes_liquidity_floor(closes, volumes)
        assert ok is False
        assert "too thin" in reason

    def test_latest_price_is_what_counts(self):
        # A name that recovered above $3 by the latest close passes the
        # price leg even if it traded below earlier in the window.
        closes = [2.0] * 29 + [5.0]
        volumes = [4_000_000] * 30  # keeps avg dollar volume above the $5M floor despite the cheap stretch
        ok, _ = passes_liquidity_floor(closes, volumes)
        assert ok is True

    def test_custom_floor_config(self):
        closes, volumes = self._series(price=4.0, volume=2_000_000)
        ok, _ = passes_liquidity_floor(closes, volumes, config=LiquidityFloorConfig(min_price=5.0, min_avg_dollar_volume=1.0))
        assert ok is False

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError):
            passes_liquidity_floor([], [])
        with pytest.raises(ValueError):
            passes_liquidity_floor([1.0, 2.0], [1.0])
        with pytest.raises(ValueError):
            LiquidityFloorConfig(min_price=0)
