"""
Deterministic position sizing + sector concentration -- applied by
run_cycle to every proposal AFTER the portfolio manager proposes and
BEFORE risk scoring/execution.

Three pieces, all pure and unit-tested (tests/test_sizing.py):

  1. scale_buy_quantity_for_regime(): buys shrink in a hostile market
     (multiplier from src.risk.regime.assess_market_regime -- 0.75
     neutral, 0.5 risk_off). The model is also TOLD the regime in its
     prompt; this is the deterministic guarantee on top, same
     suggestion-vs-rule relationship as churn_guard to the memory prompt.
  2. scale_buy_quantity_for_volatility(): a buy in a name running hotter
     than `max_vol_ratio` times benchmark volatility is scaled down
     proportionally (a 4x-vol name at a 2.0 cap gets half the shares), so
     every position contributes roughly comparable risk rather than
     comparable dollars. This reuses the asset/benchmark vols run_cycle
     already fetches per proposal for the risk scorer -- no new data.
  3. evaluate_sector_concentration(): a buy that would push one sector
     past `max_sector_pct` of the portfolio is BLOCKED (not scaled --
     concentration is a threshold problem, not a sizing problem). The
     motivating failure mode: five individually-reasonable adds that are
     all secretly the same sector bet. Sector comes from yfinance
     fundamentals (src/signals/fundamentals.py), fetched once per cycle;
     a symbol with UNKNOWN sector is allowed through with that fact in
     the reasoning -- yfinance is flaky, and silently blocking trades on
     a data vendor hiccup is a worse failure than occasionally letting an
     unclassifiable symbol through (the 15% per-position rail still
     applies to it regardless).

Sells are never scaled and never sector-blocked -- exits reduce risk by
definition, and every rule in this repo that touches exits follows the
same principle: never make it structurally hard to get OUT.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.signals.fundamentals import MarketRegime

DEFAULT_MAX_VOL_RATIO = 2.0
DEFAULT_MAX_SECTOR_PCT = 0.30


def scale_buy_quantity_for_regime(quantity: float, side: str, regime: MarketRegime | None) -> float:
    """Regime-scaled buy quantity. Sells pass through untouched; a missing
    regime read means no scaling (an unavailable data feed must never
    silently shrink orders). Results are floored to 1 share minimum --
    scaling a 1-share buy to 0.5 shares would turn 'size down' into
    'silently cancel', which is churn-guard/audit territory, not sizing
    territory."""
    if side != "buy" or regime is None or regime.sizing_multiplier >= 1.0:
        return quantity
    return max(1.0, round(quantity * regime.sizing_multiplier))


def scale_buy_quantity_for_volatility(
    quantity: float,
    side: str,
    asset_30d_volatility: float,
    benchmark_30d_volatility: float,
    max_vol_ratio: float = DEFAULT_MAX_VOL_RATIO,
) -> float:
    """Volatility-normalized buy quantity: names within max_vol_ratio of
    benchmark volatility are untouched; hotter names shrink by
    (max_vol_ratio / actual_ratio). Same 1-share floor as regime scaling."""
    if side != "buy" or benchmark_30d_volatility <= 0 or asset_30d_volatility <= 0:
        return quantity
    ratio = asset_30d_volatility / benchmark_30d_volatility
    if ratio <= max_vol_ratio:
        return quantity
    return max(1.0, round(quantity * (max_vol_ratio / ratio)))


@dataclass(frozen=True)
class SectorDecision:
    allowed: bool
    reason: str


def evaluate_sector_concentration(
    side: str,
    trade_value: float,
    symbol_sector: str | None,
    sector_exposure_value: dict[str, float],
    total_portfolio_value: float,
    max_sector_pct: float = DEFAULT_MAX_SECTOR_PCT,
) -> SectorDecision:
    """Would this buy push its sector past max_sector_pct of the portfolio?

    sector_exposure_value maps sector name -> current $ market value held
    in that sector (built by run_cycle from current positions + the cycle's
    fundamentals fetch). Unknown-sector symbols are allowed (see module
    docstring)."""
    if total_portfolio_value <= 0:
        raise ValueError("total_portfolio_value must be > 0")
    if side != "buy":
        return SectorDecision(allowed=True, reason="sells are never sector-blocked")
    if symbol_sector is None:
        return SectorDecision(
            allowed=True,
            reason="sector unknown for this symbol (fundamentals unavailable) -- allowed, per-position rails still apply",
        )

    current = sector_exposure_value.get(symbol_sector, 0.0)
    resulting_pct = (current + trade_value) / total_portfolio_value
    if resulting_pct > max_sector_pct:
        return SectorDecision(
            allowed=False,
            reason=(
                f"buy of ${trade_value:,.2f} would take {symbol_sector} exposure to {resulting_pct:.1%} "
                f"of portfolio, past the {max_sector_pct:.0%} sector cap "
                f"(currently ${current:,.2f} = {current / total_portfolio_value:.1%})"
            ),
        )
    return SectorDecision(
        allowed=True,
        reason=f"{symbol_sector} exposure would be {resulting_pct:.1%} of portfolio, within the {max_sector_pct:.0%} cap",
    )
