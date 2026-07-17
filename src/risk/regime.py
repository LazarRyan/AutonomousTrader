"""
Market regime assessment -- is this a market to be adding risk into?

The per-symbol pipeline (signals -> blend -> propose -> score -> rails)
has no concept of the MARKET being hostile: on a day SPY is breaking down
and the VIX is spiking, a stock-specific bullish signal is a materially
worse bet than the same signal in a calm uptrend, and the old pipeline
treated them identically.

Output is used in two places:
  1. The portfolio manager prompt (via
     src.signals.fundamentals.render_market_context) -- soft guidance,
     the model is told to propose more conservatively in risk_off.
  2. Deterministic buy-quantity scaling in run_cycle -- hard guarantee:
     executed buy quantities are multiplied by sizing_multiplier BEFORE
     risk scoring, so risk_off actually shrinks orders even if the model
     ignores the guidance. Sells are never scaled (never make it harder
     to exit -- same principle as churn_guard's uncapped sells).

Classification (assess_market_regime, pure, unit-tested):

    risk_off  -- SPY below its 50-day SMA AND VIX >= 25, or VIX >= 30 alone
                 (a vol spike that fast is risk-off whatever the SMA says).
                 sizing_multiplier 0.5.
    neutral   -- exactly one of (SPY below SMA, VIX >= 20). multiplier 0.75.
    risk_on   -- SPY at/above its 50-day SMA and VIX < 20. multiplier 1.0.

Thresholds are ChatGPT-obvious on purpose: this is a blunt "don't lean in
during a storm" instrument, not an alpha source, and blunt instruments
should be legible. VIX data comes from yfinance (^VIX -- Alpaca doesn't
serve index quotes on the free feed); SPY closes come from the existing
Alpaca fetch. Both best-effort: no VIX means classification falls back to
the SMA test alone (documented in the reasoning string), no SPY closes
means no assessment at all (None -- callers treat that as multiplier 1.0,
i.e. an unavailable regime read never silently shrinks orders).
"""

from __future__ import annotations

from src.signals.fundamentals import MarketRegime

SMA_WINDOW = 50
VIX_ELEVATED = 20.0
VIX_HIGH = 25.0
VIX_SPIKE = 30.0

RISK_OFF_MULTIPLIER = 0.5
NEUTRAL_MULTIPLIER = 0.75
RISK_ON_MULTIPLIER = 1.0


def assess_market_regime(spy_closes: list[float], vix_level: float | None) -> MarketRegime:
    """Classify the current regime from SPY daily closes (oldest-first,
    needs >= SMA_WINDOW values) and an optional VIX level. Pure,
    unit-tested."""
    if len(spy_closes) < SMA_WINDOW:
        raise ValueError(f"need at least {SMA_WINDOW} SPY closes for a {SMA_WINDOW}-day SMA, got {len(spy_closes)}")

    sma = sum(spy_closes[-SMA_WINDOW:]) / SMA_WINDOW
    latest = spy_closes[-1]
    below_sma = latest < sma
    trend_text = f"SPY {latest:.2f} {'below' if below_sma else 'at/above'} its {SMA_WINDOW}d SMA ({sma:.2f})"

    if vix_level is None:
        # SMA-only fallback -- can distinguish risk_on from neutral, but
        # never claims risk_off on trend alone (a drawdown with low vol is
        # a drift, not a storm; halting-sized reactions need vol evidence).
        if below_sma:
            return MarketRegime("neutral", NEUTRAL_MULTIPLIER, f"{trend_text}; VIX unavailable (SMA-only assessment)")
        return MarketRegime("risk_on", RISK_ON_MULTIPLIER, f"{trend_text}; VIX unavailable (SMA-only assessment)")

    vix_text = f"VIX {vix_level:.1f}"

    if vix_level >= VIX_SPIKE or (below_sma and vix_level >= VIX_HIGH):
        return MarketRegime("risk_off", RISK_OFF_MULTIPLIER, f"{trend_text}; {vix_text}")
    if below_sma or vix_level >= VIX_ELEVATED:
        return MarketRegime("neutral", NEUTRAL_MULTIPLIER, f"{trend_text}; {vix_text}")
    return MarketRegime("risk_on", RISK_ON_MULTIPLIER, f"{trend_text}; {vix_text}")


# ============================================================
# Thin glue -- VIX fetch via yfinance. SPY closes come from the caller's
# existing Alpaca fetch (src.signals.momentum.fetch_daily_closes).
# ============================================================


def fetch_vix_level() -> float | None:
    """Latest ^VIX close, best-effort. None on any failure -- see module
    docstring for how classification degrades."""
    try:
        import yfinance as yf

        history = yf.Ticker("^VIX").history(period="5d")
        if history is None or history.empty:
            return None
        return float(history["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001 -- regime context, never a blocker
        print(f"VIX fetch failed, regime assessment will be SMA-only: {exc}")
        return None
