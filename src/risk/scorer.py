"""
Deterministic composite risk scorer.

No LLM involved anywhere in this file. Given a candidate trade and portfolio
context, produces a 0-100+ composite risk score and a boolean "needs_approval"
decision. This is the gate between the Portfolio Manager Agent (which proposes
trades) and the Execution Agent (which fires them).

Formula (see build plan section 5):

    composite_risk_score =
        0.5 * size_component        (trade_value / total_portfolio_value, as 0-100)
      + 0.3 * volatility_component  (asset_30d_volatility / benchmark_volatility, as 0-100)
      + 0.2 * liquidity_component   (liquidity penalty, already 0-100, higher = thinner/riskier)

Hard override: any single trade > 5% of portfolio value ALWAYS needs approval,
regardless of composite score.

Threshold: needs_approval if composite_risk_score >= 70, OR the hard override
above fires -- whichever triggers first.

Both the weights and the threshold are configurable (see RiskScorerConfig) so
they can be tuned after backtesting without touching this logic.

KNOWN TENSION IN THE DEFAULTS (see tests/test_scorer.py for detail): with the
default weights (0.5/0.3/0.2), a trade sized at exactly the 5% hard-override
boundary maxes out at composite_score = 0.5*5 + 0.3*100 + 0.2*100 = 52.5. So
the default approval_threshold of 70 can, in practice, only be reached
together with the hard override -- never independently of it. Not a bug, but
worth knowing before tuning: if an independently meaningful composite-score
threshold is wanted, either lower approval_threshold or reweight so
volatility/liquidity can push the score past 70 on their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskScorerConfig:
    size_weight: float = 0.5
    volatility_weight: float = 0.3
    liquidity_weight: float = 0.2

    # Composite score at/above this value requires human approval.
    approval_threshold: float = 70.0

    # Any single trade above this fraction of portfolio value ALWAYS requires
    # approval, regardless of composite score. Expressed as a fraction (0.05 = 5%).
    hard_override_position_pct: float = 0.05

    # Volatility ratio is uncapped in the raw math but we cap its contribution
    # to the composite score so one extreme-vol name can't silently swamp the
    # formula. 3.0 means "3x benchmark volatility" is the max counted ratio.
    volatility_ratio_cap: float = 3.0

    def __post_init__(self) -> None:
        total = self.size_weight + self.volatility_weight + self.liquidity_weight
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Risk scorer weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class TradeRiskInputs:
    """Everything the scorer needs about one candidate trade."""

    symbol: str
    trade_value: float           # absolute $ value of the proposed trade
    total_portfolio_value: float  # current total portfolio value ($)
    asset_30d_volatility: float   # e.g. stdev of daily returns, last 30d
    benchmark_30d_volatility: float  # same metric for the benchmark (e.g. SPY)
    liquidity_penalty: float      # 0-100, precomputed elsewhere (thin/low-volume = high)


@dataclass(frozen=True)
class RiskScoreResult:
    symbol: str
    composite_score: float
    size_component: float
    volatility_component: float
    liquidity_component: float
    position_pct: float
    hard_override_triggered: bool
    needs_approval: bool
    reasoning: str


def score_trade(inputs: TradeRiskInputs, config: RiskScorerConfig | None = None) -> RiskScoreResult:
    """Compute the composite risk score and approval decision for one trade.

    Raises ValueError on nonsensical inputs (negative values, zero portfolio,
    etc.) rather than silently producing a misleading score -- a bad risk
    number here is worse than a loud crash.
    """
    config = config or RiskScorerConfig()

    if inputs.total_portfolio_value <= 0:
        raise ValueError("total_portfolio_value must be > 0")
    if inputs.trade_value < 0:
        raise ValueError("trade_value must be >= 0")
    if inputs.benchmark_30d_volatility <= 0:
        raise ValueError("benchmark_30d_volatility must be > 0")
    if inputs.asset_30d_volatility < 0:
        raise ValueError("asset_30d_volatility must be >= 0")
    if not (0.0 <= inputs.liquidity_penalty <= 100.0):
        raise ValueError("liquidity_penalty must be within 0-100")

    position_pct = inputs.trade_value / inputs.total_portfolio_value

    # --- size component: 0-100, capped at 100 (i.e. a trade >= 100% of the
    # portfolio maxes out this component; it will already be blocked upstream
    # by safety_rails long before it gets here in practice).
    size_component = min(position_pct, 1.0) * 100.0

    # --- volatility component: ratio to benchmark, capped, scaled to 0-100.
    raw_vol_ratio = inputs.asset_30d_volatility / inputs.benchmark_30d_volatility
    capped_vol_ratio = min(raw_vol_ratio, config.volatility_ratio_cap)
    volatility_component = (capped_vol_ratio / config.volatility_ratio_cap) * 100.0

    # --- liquidity component: already 0-100.
    liquidity_component = inputs.liquidity_penalty

    composite_score = (
        config.size_weight * size_component
        + config.volatility_weight * volatility_component
        + config.liquidity_weight * liquidity_component
    )

    hard_override_triggered = position_pct > config.hard_override_position_pct
    needs_approval = hard_override_triggered or composite_score >= config.approval_threshold

    reasons = []
    if hard_override_triggered:
        reasons.append(
            f"position size {position_pct:.2%} exceeds hard override threshold "
            f"{config.hard_override_position_pct:.2%}"
        )
    if composite_score >= config.approval_threshold:
        reasons.append(
            f"composite score {composite_score:.1f} >= approval threshold "
            f"{config.approval_threshold:.1f}"
        )
    if not reasons:
        reasons.append(
            f"composite score {composite_score:.1f} below threshold "
            f"{config.approval_threshold:.1f} and position size "
            f"{position_pct:.2%} within limit -- eligible for auto-execution"
        )
    reasoning = "; ".join(reasons)

    return RiskScoreResult(
        symbol=inputs.symbol,
        composite_score=composite_score,
        size_component=size_component,
        volatility_component=volatility_component,
        liquidity_component=liquidity_component,
        position_pct=position_pct,
        hard_override_triggered=hard_override_triggered,
        needs_approval=needs_approval,
        reasoning=reasoning,
    )
