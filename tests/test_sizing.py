import pytest

from src.risk.regime import assess_market_regime
from src.risk.sizing import (
    evaluate_sector_concentration,
    scale_buy_quantity_for_regime,
    scale_buy_quantity_for_volatility,
)
from src.signals.fundamentals import MarketRegime

RISK_OFF = MarketRegime("risk_off", 0.5, "test")
RISK_ON = MarketRegime("risk_on", 1.0, "test")


class TestRegimeScaling:
    def test_risk_off_halves_buys(self):
        assert scale_buy_quantity_for_regime(20.0, "buy", RISK_OFF) == 10.0

    def test_risk_on_and_missing_regime_untouched(self):
        assert scale_buy_quantity_for_regime(20.0, "buy", RISK_ON) == 20.0
        assert scale_buy_quantity_for_regime(20.0, "buy", None) == 20.0

    def test_sells_never_scaled(self):
        assert scale_buy_quantity_for_regime(20.0, "sell", RISK_OFF) == 20.0

    def test_one_share_floor(self):
        assert scale_buy_quantity_for_regime(1.0, "buy", RISK_OFF) == 1.0


class TestVolatilityScaling:
    def test_hot_name_scaled_down_proportionally(self):
        # 4x benchmark vol at a 2.0 cap -> half the shares.
        assert scale_buy_quantity_for_volatility(20.0, "buy", 0.04, 0.01) == 10.0

    def test_within_ratio_untouched(self):
        assert scale_buy_quantity_for_volatility(20.0, "buy", 0.015, 0.01) == 20.0

    def test_sells_and_degenerate_vols_untouched(self):
        assert scale_buy_quantity_for_volatility(20.0, "sell", 0.04, 0.01) == 20.0
        assert scale_buy_quantity_for_volatility(20.0, "buy", 0.0, 0.01) == 20.0
        assert scale_buy_quantity_for_volatility(20.0, "buy", 0.04, 0.0) == 20.0

    def test_one_share_floor(self):
        assert scale_buy_quantity_for_volatility(1.0, "buy", 0.10, 0.01) == 1.0


class TestSectorConcentration:
    def test_buy_past_cap_blocked_with_numbers_in_reason(self):
        decision = evaluate_sector_concentration(
            side="buy", trade_value=5_000.0, symbol_sector="Technology",
            sector_exposure_value={"Technology": 27_000.0}, total_portfolio_value=100_000.0,
        )
        assert decision.allowed is False
        assert "32.0%" in decision.reason and "30%" in decision.reason

    def test_buy_within_cap_allowed(self):
        decision = evaluate_sector_concentration(
            side="buy", trade_value=1_000.0, symbol_sector="Technology",
            sector_exposure_value={"Technology": 27_000.0}, total_portfolio_value=100_000.0,
        )
        assert decision.allowed is True

    def test_sells_never_blocked(self):
        decision = evaluate_sector_concentration(
            side="sell", trade_value=50_000.0, symbol_sector="Technology",
            sector_exposure_value={"Technology": 90_000.0}, total_portfolio_value=100_000.0,
        )
        assert decision.allowed is True

    def test_unknown_sector_allowed_with_explanation(self):
        decision = evaluate_sector_concentration(
            side="buy", trade_value=50_000.0, symbol_sector=None,
            sector_exposure_value={}, total_portfolio_value=100_000.0,
        )
        assert decision.allowed is True
        assert "sector unknown" in decision.reason

    def test_zero_portfolio_raises(self):
        with pytest.raises(ValueError):
            evaluate_sector_concentration("buy", 1.0, "Tech", {}, 0.0)


class TestRegimeAssessment:
    def _closes(self, level: float, n: int = 60) -> list[float]:
        return [level] * n

    def test_calm_uptrend_is_risk_on(self):
        closes = self._closes(100.0)
        closes[-1] = 101.0
        regime = assess_market_regime(closes, vix_level=14.0)
        assert regime.label == "risk_on" and regime.sizing_multiplier == 1.0

    def test_below_sma_with_high_vix_is_risk_off(self):
        closes = self._closes(100.0)
        closes[-1] = 90.0
        regime = assess_market_regime(closes, vix_level=27.0)
        assert regime.label == "risk_off" and regime.sizing_multiplier == 0.5

    def test_vix_spike_alone_is_risk_off_even_above_sma(self):
        closes = self._closes(100.0)
        closes[-1] = 105.0
        assert assess_market_regime(closes, vix_level=33.0).label == "risk_off"

    def test_one_warning_flag_is_neutral(self):
        below = self._closes(100.0)
        below[-1] = 90.0
        assert assess_market_regime(below, vix_level=12.0).label == "neutral"
        above = self._closes(100.0)
        above[-1] = 105.0
        assert assess_market_regime(above, vix_level=22.0).label == "neutral"

    def test_no_vix_never_claims_risk_off(self):
        closes = self._closes(100.0)
        closes[-1] = 80.0
        regime = assess_market_regime(closes, vix_level=None)
        assert regime.label == "neutral"
        assert "SMA-only" in regime.reasoning

    def test_too_few_closes_raises(self):
        with pytest.raises(ValueError):
            assess_market_regime([100.0] * 10, vix_level=15.0)
