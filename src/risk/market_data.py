"""
Real volatility and liquidity inputs for the risk scorer.

risk/scorer.py takes asset_30d_volatility, benchmark_30d_volatility, and
liquidity_penalty as inputs -- until now, src/main.py fed it neutral
placeholders (0.02 / 0.02 / 0.0) so the pipeline was complete end-to-end
while this piece wasn't built yet. This module replaces those placeholders
with real calculations from actual historical bars. This was always pure
arithmetic waiting to be written, not something that needed "time to pass"
-- same two-layer split as every other signal module in this project:

  1. PURE, unit-tested math: compute_volatility() (stdev of daily simple
     returns) and compute_liquidity_penalty() (0-100 penalty from average
     daily dollar volume, scaled against a configurable full-liquidity
     threshold). Both covered by tests/test_market_data.py with synthetic
     price/volume series -- no network needed.
  2. Thin network wrapper: fetch_bars_with_volume() -- Alpaca historical
     bars including volume, same pattern as signals/momentum.py's
     fetch_daily_closes. Not unit-tested (needs live network + keys).

Both asset and benchmark volatility must be computed with the SAME method
over the SAME window for their ratio (which is all risk/scorer.py actually
uses) to mean anything -- compute_volatility() takes no annualization or
window-specific parameters for that reason; whatever window of closes you
pass in defines "the window," and callers must pass equal-length,
same-period windows for both asset and benchmark.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


def compute_volatility(closes: list[float]) -> float:
    """Standard deviation of daily simple returns over the given closes.
    Not annualized -- risk/scorer.py only uses the asset/benchmark RATIO,
    which is annualization-invariant as long as both sides use this same
    function over comparable windows. Raises ValueError on insufficient or
    invalid data rather than returning a misleading number.
    """
    if len(closes) < 3:
        raise ValueError(f"Need at least 3 closes to compute volatility, got {len(closes)}")
    if any(c <= 0 for c in closes):
        raise ValueError("All closes must be > 0")

    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    return statistics.stdev(returns)


@dataclass(frozen=True)
class LiquidityConfig:
    # Average daily dollar volume (price * volume) at/above which a symbol
    # is considered fully liquid -- 0 penalty. Below this, penalty scales
    # up linearly to 100 at zero volume.
    full_liquidity_dollar_volume: float = 10_000_000.0

    def __post_init__(self) -> None:
        if self.full_liquidity_dollar_volume <= 0:
            raise ValueError("full_liquidity_dollar_volume must be > 0")


def compute_liquidity_penalty(
    closes: list[float], volumes: list[float], config: LiquidityConfig | None = None
) -> float:
    """0-100 liquidity penalty (higher = thinner/riskier) from average daily
    dollar volume over the given window. Raises ValueError on mismatched
    lengths, empty input, or negative values -- never guesses.
    """
    config = config or LiquidityConfig()

    if len(closes) != len(volumes):
        raise ValueError(f"closes and volumes must be the same length, got {len(closes)} and {len(volumes)}")
    if not closes:
        raise ValueError("closes/volumes must not be empty")
    if any(c <= 0 for c in closes):
        raise ValueError("All closes must be > 0")
    if any(v < 0 for v in volumes):
        raise ValueError("All volumes must be >= 0")

    dollar_volumes = [c * v for c, v in zip(closes, volumes)]
    avg_dollar_volume = statistics.mean(dollar_volumes)

    if avg_dollar_volume >= config.full_liquidity_dollar_volume:
        return 0.0

    ratio = avg_dollar_volume / config.full_liquidity_dollar_volume
    penalty = (1.0 - ratio) * 100.0
    return max(0.0, min(100.0, penalty))


@dataclass(frozen=True)
class LiquidityFloorConfig:
    """The hard data-quality floor for the full-market universe (2026-07-16,
    added alongside the everything-tradable expansion in src/universe.py).
    Distinct in kind from compute_liquidity_penalty above: the penalty is a
    graded risk INPUT for scoring, this is a binary eligibility test --
    below the floor, price/volume data is too degenerate for any of this
    pipeline's signals to mean anything (spreads swamp momentum, one trade
    moves 'sentiment'), so the trade is skipped outright rather than merely
    scored as risky."""

    min_price: float = 3.0
    min_avg_dollar_volume: float = 5_000_000.0

    def __post_init__(self) -> None:
        if self.min_price <= 0:
            raise ValueError("min_price must be > 0")
        if self.min_avg_dollar_volume <= 0:
            raise ValueError("min_avg_dollar_volume must be > 0")


def passes_liquidity_floor(
    closes: list[float], volumes: list[float], config: LiquidityFloorConfig | None = None
) -> tuple[bool, str]:
    """(passes, reason) for the hard floor above. Same input validation
    posture as compute_liquidity_penalty: mismatched/empty/invalid series
    raise rather than guess. Pure, unit-tested."""
    config = config or LiquidityFloorConfig()

    if len(closes) != len(volumes):
        raise ValueError(f"closes and volumes must be the same length, got {len(closes)} and {len(volumes)}")
    if not closes:
        raise ValueError("closes/volumes must not be empty")
    if any(c <= 0 for c in closes):
        raise ValueError("All closes must be > 0")
    if any(v < 0 for v in volumes):
        raise ValueError("All volumes must be >= 0")

    latest_price = closes[-1]
    avg_dollar_volume = statistics.mean(c * v for c, v in zip(closes, volumes))

    if latest_price < config.min_price:
        return False, (
            f"price ${latest_price:.2f} is below the ${config.min_price:.2f} floor -- "
            f"sub-${config.min_price:.0f} names have spread/data quality this pipeline can't price honestly"
        )
    if avg_dollar_volume < config.min_avg_dollar_volume:
        return False, (
            f"avg daily dollar volume ${avg_dollar_volume:,.0f} is below the "
            f"${config.min_avg_dollar_volume:,.0f} floor -- too thin to trade on these signals"
        )
    return True, (
        f"price ${latest_price:.2f} and avg daily dollar volume ${avg_dollar_volume:,.0f} clear the floor"
    )


# ============================================================
# Network wrapper -- not unit-tested here (requires live Alpaca network +
# keys). See signals/momentum.py's fetch_daily_closes for the same pattern.
# ============================================================


def fetch_bars_with_volume(
    symbol: str, api_key: str, secret_key: str, lookback_days: int = 30
) -> tuple[list[float], list[float]]:
    """Fetch daily (closes, volumes) from Alpaca's historical bars API,
    oldest first, for use with compute_volatility() and
    compute_liquidity_penalty() above.
    """
    from datetime import datetime, timedelta, timezone

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(api_key, secret_key)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lookback_days * 1.6) + 5)

    # feed=IEX explicitly -- see the identical comment in signals/momentum.py's
    # fetch_daily_closes. alpaca-py defaults to SIP, which a basic/free
    # market-data subscription cannot query for recent data.
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX
    )
    bars = client.get_stock_bars(request)

    df = bars.df
    if df is None or df.empty:
        raise ValueError(f"No bar data returned for {symbol}")

    closes = df["close"].tolist()[-lookback_days:]
    volumes = df["volume"].tolist()[-lookback_days:]
    return closes, volumes
