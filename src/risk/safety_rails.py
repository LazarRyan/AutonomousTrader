"""
Non-negotiable safety rails.

These are the hard backstops from build plan section 6. They are enforced
by the Execution Agent before EVERY order, regardless of what the risk
scorer or a human approval decided. Nothing here is a "suggestion" --
if evaluate_trade() returns allowed=False, the trade does not go out.

Relationship to risk/scorer.py: the risk scorer decides whether a trade
needs human approval (composite score / 5% position override -> queue).
Safety rails are a separate, stricter layer: even a human-approved trade
still has to clear the max position size cap and the loss-limit halts
below. A human approving a queued trade cannot override a safety rail --
only an explicit config change (position size, loss limits) or an explicit
kill-switch/halt reset can do that.

IMPORTANT, found via a real dry run: risk/scorer.py's hard_override_position_pct
(5%, triggers approval) and this module's max_position_pct used to BOTH
default to 0.05. Since they were identical, ANY trade routed to the
approval queue for exceeding 5% position size was mathematically
guaranteed to be blocked here regardless of the human's decision -- there
was no size range where an approval could ever actually result in
execution (confirmed live: ABBV/AEP/ALGN were all approved via the CLI
watcher, then all blocked here a moment later). max_position_pct is now
0.15 specifically to leave a real 5%-15% band where a human approval can
lead to execution; anything above 15% remains an absolute, non-negotiable
block no matter what a human decides.

Everything here is deterministic and unit-tested. No LLM involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum


class HaltReason(str, Enum):
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    MAX_POSITION_SIZE = "max_position_size"


@dataclass(frozen=True)
class SafetyConfig:
    # Max size of any single trade, as a fraction of total portfolio value.
    # This is the absolute, non-negotiable ceiling -- distinct from
    # risk/scorer.py's hard_override_position_pct (0.05), which only decides
    # whether a trade needs human approval. Deliberately set higher than
    # that so there's a real 5%-15% band where an approval can actually
    # result in execution (see module docstring for the real bug this fixes).
    max_position_pct: float = 0.15

    # Daily realized+unrealized loss, as a fraction of portfolio value, that
    # triggers an automatic halt for the REST OF THE DAY. Resets automatically
    # at the start of the next trading day.
    max_daily_loss_pct: float = 0.03

    # Weekly loss, as a fraction of portfolio value, that triggers a halt
    # requiring a MANUAL reset (does not auto-clear at the start of a new week).
    max_weekly_loss_pct: float = 0.08

    def __post_init__(self) -> None:
        for name in ("max_position_pct", "max_daily_loss_pct", "max_weekly_loss_pct"):
            value = getattr(self, name)
            if not (0.0 < value <= 1.0):
                raise ValueError(f"{name} must be within (0, 1], got {value}")


@dataclass
class SafetyState:
    """Mutable, persisted state (mirrors the `safety_state` Supabase table).

    One row of this should exist in the DB at all times. The execution agent
    loads it fresh before every trade decision -- never caches it across
    cycles, since a human may have flipped the kill switch or hit a loss
    limit moments ago.
    """

    kill_switch_engaged: bool = False
    daily_pnl_pct: float = 0.0          # today's P&L as a fraction of portfolio value (negative = loss)
    weekly_pnl_pct: float = 0.0         # this week's P&L as a fraction of portfolio value
    daily_halted: bool = False
    weekly_halted: bool = False
    last_daily_reset: date | None = None
    last_weekly_reset: date | None = None

    def reset_daily(self, today: date | None = None) -> None:
        """Auto-called at the start of each new trading day."""
        self.daily_pnl_pct = 0.0
        self.daily_halted = False
        self.last_daily_reset = today or datetime.now(timezone.utc).date()

    def reset_weekly(self, today: date | None = None) -> None:
        """Manual only -- must be called explicitly by Ryan, never automatically."""
        self.weekly_pnl_pct = 0.0
        self.weekly_halted = False
        self.last_weekly_reset = today or datetime.now(timezone.utc).date()

    def record_pnl(self, daily_pnl_pct: float, weekly_pnl_pct: float, config: SafetyConfig) -> None:
        """Update tracked P&L and flip halt flags if limits are breached.

        Loss limits are one-way latches within their period: once halted,
        stays halted until the relevant reset (auto for daily, manual for
        weekly) -- a bounce back above the loss threshold mid-day does not
        silently un-halt trading.
        """
        self.daily_pnl_pct = daily_pnl_pct
        self.weekly_pnl_pct = weekly_pnl_pct

        if daily_pnl_pct <= -config.max_daily_loss_pct:
            self.daily_halted = True
        if weekly_pnl_pct <= -config.max_weekly_loss_pct:
            self.weekly_halted = True


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reasoning(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "all safety rails clear"


def evaluate_trade(
    trade_value: float,
    total_portfolio_value: float,
    state: SafetyState,
    config: SafetyConfig | None = None,
) -> SafetyDecision:
    """The single choke point every order must pass through before firing.

    Checks, in order (all are checked, not just the first failure, so the
    audit log captures every reason a trade was blocked):
      1. Kill switch
      2. Daily loss halt
      3. Weekly loss halt
      4. Max position size

    Returns allowed=True only if ALL checks pass.
    """
    config = config or SafetyConfig()

    if total_portfolio_value <= 0:
        raise ValueError("total_portfolio_value must be > 0")
    if trade_value < 0:
        raise ValueError("trade_value must be >= 0")

    reasons: list[str] = []

    if state.kill_switch_engaged:
        reasons.append(f"{HaltReason.KILL_SWITCH.value}: kill switch is engaged, all trading refused")

    if state.daily_halted:
        reasons.append(
            f"{HaltReason.DAILY_LOSS_LIMIT.value}: daily loss halt is active "
            f"(daily P&L {state.daily_pnl_pct:.2%}, limit -{config.max_daily_loss_pct:.2%})"
        )

    if state.weekly_halted:
        reasons.append(
            f"{HaltReason.WEEKLY_LOSS_LIMIT.value}: weekly loss halt is active "
            f"(weekly P&L {state.weekly_pnl_pct:.2%}, limit -{config.max_weekly_loss_pct:.2%}) "
            f"-- requires manual reset"
        )

    position_pct = trade_value / total_portfolio_value
    if position_pct > config.max_position_pct:
        reasons.append(
            f"{HaltReason.MAX_POSITION_SIZE.value}: trade is {position_pct:.2%} of portfolio, "
            f"exceeds max position size {config.max_position_pct:.2%}"
        )

    return SafetyDecision(allowed=(len(reasons) == 0), reasons=reasons)
