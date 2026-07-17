from datetime import date

from src.signals.fundamentals import (
    FundamentalsSnapshot,
    MarketRegime,
    days_until,
    render_market_context,
)

TODAY = date(2026, 7, 16)


class TestDaysUntil:
    def test_future_past_and_missing(self):
        assert days_until(date(2026, 7, 20), TODAY) == 4
        assert days_until(date(2026, 7, 10), TODAY) == -6
        assert days_until(None, TODAY) is None


class TestRenderMarketContext:
    def test_imminent_earnings_shouts(self):
        snapshot = FundamentalsSnapshot(symbol="HUM", next_earnings_date=date(2026, 7, 19))
        text = render_market_context([snapshot], None, TODAY)
        assert "EARNINGS IN 3 DAY(S) (2026-07-19)" in text

    def test_distant_earnings_calm(self):
        snapshot = FundamentalsSnapshot(symbol="HUM", next_earnings_date=date(2026, 9, 1))
        text = render_market_context([snapshot], None, TODAY)
        assert "next earnings 2026-09-01" in text
        assert "EARNINGS IN" not in text

    def test_stale_earnings_date_rendered_as_recent(self):
        snapshot = FundamentalsSnapshot(symbol="HUM", next_earnings_date=date(2026, 7, 1))
        assert "earnings recently reported" in render_market_context([snapshot], None, TODAY)

    def test_full_fundamentals_line(self):
        snapshot = FundamentalsSnapshot(
            symbol="KHC", sector="Consumer Defensive", trailing_pe=12.3, forward_pe=11.1, revenue_growth=0.042
        )
        text = render_market_context([snapshot], None, TODAY)
        assert "KHC: sector: Consumer Defensive, trailing P/E 12.3, forward P/E 11.1, revenue growth +4.2% yoy" in text

    def test_all_none_snapshot_says_so(self):
        assert "(no fundamentals available)" in render_market_context([FundamentalsSnapshot(symbol="XYZ")], None, TODAY)

    def test_empty_inputs_get_placeholders(self):
        text = render_market_context([], None, TODAY)
        assert "no fundamentals available this cycle" in text
        assert "regime assessment unavailable this cycle" in text

    def test_regime_rendered_with_multiplier(self):
        regime = MarketRegime("risk_off", 0.5, "SPY below SMA; VIX 31.0")
        text = render_market_context([], regime, TODAY)
        assert "risk_off (sizing multiplier 0.5): SPY below SMA; VIX 31.0" in text

    def test_symbols_sorted(self):
        snapshots = [FundamentalsSnapshot(symbol="ZTS"), FundamentalsSnapshot(symbol="AAPL")]
        text = render_market_context(snapshots, None, TODAY)
        assert text.index("AAPL") < text.index("ZTS")
