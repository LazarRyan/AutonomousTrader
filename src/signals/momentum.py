"""
Deterministic momentum signal. No LLM anywhere in this file.

Combines three classic technical indicators, computed from daily close
prices, into a single continuous momentum score in [-100, 100] (positive =
bullish, negative = bearish, 0 = neutral):

  1. SMA(20)/SMA(50) crossover -- trend direction, expressed as the
     percentage gap between the short and long moving average.
  2. RSI(14) -- overbought/oversold, remapped from its native 0-100 scale
     onto -100..100 around a neutral midpoint of 50.
  3. 10-day rate of change -- short-term price momentum.

All three are pure functions over a list of closes. The only part of this
module that talks to the network is `fetch_daily_closes`, which is a thin
Alpaca historical-bars wrapper (same pattern as investment-monitor's
`get_stock_bars`) -- it is NOT unit-tested here because doing so would
require live Alpaca keys and network access; the scoring math above it is
fully covered by tests/test_momentum.py using synthetic price series.

Weights and windows are configurable (MomentumConfig) so they can be tuned
after backtesting without touching this logic, same discipline as
risk/scorer.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MomentumConfig:
    sma_short_window: int = 20
    sma_long_window: int = 50
    rsi_period: int = 14
    roc_period: int = 10

    trend_weight: float = 0.4
    rsi_weight: float = 0.3
    roc_weight: float = 0.3

    # Percentage gap (short SMA vs long SMA) that maps to a full +/-100 on
    # the trend component. E.g. 10.0 means a 10% gap is already "maximally
    # bullish/bearish" for scoring purposes.
    trend_cap_pct: float = 10.0

    # 10-day rate-of-change percentage that maps to a full +/-100 on the
    # ROC component.
    roc_cap_pct: float = 10.0

    def __post_init__(self) -> None:
        total = self.trend_weight + self.rsi_weight + self.roc_weight
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Momentum weights must sum to 1.0, got {total}")
        if self.sma_short_window >= self.sma_long_window:
            raise ValueError("sma_short_window must be < sma_long_window")
        if self.rsi_period < 2:
            raise ValueError("rsi_period must be >= 2")
        if self.roc_period < 1:
            raise ValueError("roc_period must be >= 1")

    @property
    def min_required_closes(self) -> int:
        # SMA(long) needs `sma_long_window` closes. RSI(n) needs n+1 closes
        # (n price changes). ROC(n) needs n+1 closes.
        return max(self.sma_long_window, self.rsi_period + 1, self.roc_period + 1)


@dataclass(frozen=True)
class MomentumScoreResult:
    symbol: str
    momentum_score: float
    trend_component: float
    rsi_component: float
    roc_component: float
    sma_short: float
    sma_long: float
    rsi: float
    roc_pct: float
    reasoning: str


def _sma(closes: list[float], window: int) -> float:
    return sum(closes[-window:]) / window


def _rsi(closes: list[float], period: int) -> float:
    """Wilder's RSI over the last `period` price changes."""
    window = closes[-(period + 1):]
    gains = []
    losses = []
    for prev, curr in zip(window, window[1:]):
        change = curr - prev
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-change)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0 and avg_gain == 0:
        return 50.0  # perfectly flat -- neutral, not "maximally strong"
    if avg_loss == 0:
        return 100.0  # every change was a gain
    if avg_gain == 0:
        return 0.0  # every change was a loss

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _rate_of_change(closes: list[float], period: int) -> float:
    reference = closes[-(period + 1)]
    current = closes[-1]
    if reference == 0:
        raise ValueError("Cannot compute rate of change: reference close is 0")
    return (current - reference) / reference * 100.0


def _clip(value: float, cap: float) -> float:
    return max(-cap, min(cap, value))


def compute_momentum_score(
    symbol: str, closes: list[float], config: MomentumConfig | None = None
) -> MomentumScoreResult:
    """Compute the composite momentum score for one symbol given a list of
    daily closes, oldest first. Raises ValueError if there isn't enough
    history for the configured windows -- never silently pads or guesses.
    """
    config = config or MomentumConfig()

    if len(closes) < config.min_required_closes:
        raise ValueError(
            f"Need at least {config.min_required_closes} closes for {symbol}, got {len(closes)}"
        )
    if any(c <= 0 for c in closes):
        raise ValueError(f"All closes must be > 0 for {symbol}")

    sma_short = _sma(closes, config.sma_short_window)
    sma_long = _sma(closes, config.sma_long_window)
    rsi = _rsi(closes, config.rsi_period)
    roc_pct = _rate_of_change(closes, config.roc_period)

    trend_gap_pct = (sma_short - sma_long) / sma_long * 100.0
    trend_component = _clip(trend_gap_pct, config.trend_cap_pct) / config.trend_cap_pct * 100.0

    rsi_component = (rsi - 50.0) * 2.0  # 0-100 -> -100..100

    roc_component = _clip(roc_pct, config.roc_cap_pct) / config.roc_cap_pct * 100.0

    momentum_score = (
        config.trend_weight * trend_component
        + config.rsi_weight * rsi_component
        + config.roc_weight * roc_component
    )

    direction = "bullish" if momentum_score > 0 else "bearish" if momentum_score < 0 else "neutral"
    reasoning = (
        f"{direction} momentum score {momentum_score:.1f}: "
        f"SMA{config.sma_short_window}={sma_short:.2f} vs SMA{config.sma_long_window}={sma_long:.2f} "
        f"({trend_gap_pct:+.2f}%), RSI{config.rsi_period}={rsi:.1f}, "
        f"{config.roc_period}d ROC={roc_pct:+.2f}%"
    )

    return MomentumScoreResult(
        symbol=symbol,
        momentum_score=momentum_score,
        trend_component=trend_component,
        rsi_component=rsi_component,
        roc_component=roc_component,
        sma_short=sma_short,
        sma_long=sma_long,
        rsi=rsi,
        roc_pct=roc_pct,
        reasoning=reasoning,
    )


def fetch_daily_closes(symbol: str, api_key: str, secret_key: str, lookback_days: int = 90) -> list[float]:
    """Fetch daily close prices from Alpaca's historical bars API, oldest
    first. Thin wrapper, same data pattern as investment-monitor's
    get_stock_bars.

    NOT unit-tested in this module -- requires live Alpaca keys and network
    access. Exercise this manually / with an integration test once real
    paper-trading keys are in .env. compute_momentum_score() above is the
    part that matters for correctness and is fully covered by
    tests/test_momentum.py using synthetic data.
    """
    from datetime import datetime, timedelta, timezone

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(api_key, secret_key)
    end = datetime.now(timezone.utc)
    # Fetch extra calendar days to comfortably cover weekends/holidays.
    start = end - timedelta(days=int(lookback_days * 1.6) + 5)

    # feed=IEX explicitly -- alpaca-py defaults to the SIP feed, which a
    # basic/free market-data subscription cannot query for recent data
    # ("subscription does not permit querying recent SIP data", confirmed
    # against a real account during the first live dry run). IEX is
    # available on the free tier and is what this project's historical
    # daily-bar signals were designed around.
    request = StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX
    )
    bars = client.get_stock_bars(request)

    df = bars.df
    if df is None or df.empty:
        raise ValueError(f"No bar data returned for {symbol}")

    closes = df["close"].tolist()[-lookback_days:]
    return closes
