"""
Anti-churn guard -- deterministic backstop against repeat trading on stale
information.

The prompt-side fix (memory in the portfolio manager prompt, see
src/memory/recall.py) ASKS the model not to re-buy on the same signal;
this module ENFORCES it, in the same spirit as risk/safety_rails.py
backstopping the risk scorer: an LLM instruction is a suggestion, a
deterministic gate is a rule. Motivating incident (candidate_trades,
2026-07-14..16): five separate KHC buys across three days, each cycle
independently rediscovering the same +36..+39 signal, concentrating the
position with zero new information.

Sits between the portfolio manager's proposals and process_candidate_trade
in run_cycle. A suppressed proposal is a real decision and gets its own
audit_log row (decision="churn_suppressed"), same discipline as every
other taken-or-not decision in the pipeline.

Rules (all deterministic, all unit-tested -- tests/test_churn_guard.py):

  1. SAME-SIDE COOLDOWN: a proposal is suppressed if the same symbol+side
     was already executed within `cooldown_hours` (default 20 -- long
     enough to block the 2nd and 3rd same-day cycles, short enough that a
     genuinely fresh next-day signal can still act)... UNLESS the blended
     score has moved by at least `new_information_score_delta` (default
     15 points) since that last execution -- a materially changed signal
     IS new information, and the whole point is "no repeat without new
     information", not "no repeat ever".
  2. MAX ADDS PER WINDOW: at most `max_same_side_executions_per_window`
     (default 2) executions of the same symbol+side within
     `window_days` (default 5) regardless of score movement -- the KHC
     incident was five adds in three days, each individually explainable,
     collectively concentration by drip. This cap has no new-information
     escape hatch on purpose: if the signal is really that good, the
     position is already sized for it after two adds.

Sells get the same treatment as buys (a bearish signal re-observed daily
would otherwise drip-liquidate a position in n identical trims -- see NKE
"sell 50" on consecutive days with the score unchanged at -2.4), except
that rule 2's cap does not apply to sells: capping exits is a risk we
don't want (never make it structurally hard to get OUT of a position).

Pure functions only -- the caller (run_cycle) supplies recent executed
trades from candidate_trades rows it already fetches for recall.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Statuses that count as "this actually happened" for churn purposes.
# Proposals that were blocked/rejected/failed don't start a cooldown --
# re-proposing something a human rejected is the approval queue's problem,
# not churn (and a blocked trade never changed the position).
_EXECUTED_STATUSES = {"executed", "auto_approved", "approved"}


@dataclass(frozen=True)
class ChurnGuardConfig:
    cooldown_hours: float = 20.0
    new_information_score_delta: float = 15.0
    max_same_side_executions_per_window: int = 2
    window_days: int = 5

    def __post_init__(self) -> None:
        if self.cooldown_hours <= 0:
            raise ValueError("cooldown_hours must be > 0")
        if self.new_information_score_delta < 0:
            raise ValueError("new_information_score_delta must be >= 0")
        if self.max_same_side_executions_per_window < 1:
            raise ValueError("max_same_side_executions_per_window must be >= 1")
        if self.window_days < 1:
            raise ValueError("window_days must be >= 1")


@dataclass(frozen=True)
class PastExecution:
    """One prior same-symbol action, as needed by the guard. Built by the
    caller from candidate_trades rows (which run_cycle already has via
    recall's fetch_recent_actions)."""

    symbol: str
    side: str
    status: str
    blended_signal_score: float | None
    executed_at: datetime  # tz-aware


@dataclass(frozen=True)
class ChurnDecision:
    allowed: bool
    reason: str


def evaluate_churn(
    symbol: str,
    side: str,
    current_blended_score: float | None,
    past_executions: list[PastExecution],
    now: datetime,
    config: ChurnGuardConfig | None = None,
) -> ChurnDecision:
    """Apply the two rules above to one proposal. Pure, unit-tested.

    `past_executions` may contain other symbols/sides/statuses -- filtering
    happens here so the caller can pass its one recall query result
    unmodified for every proposal in the cycle.
    """
    config = config or ChurnGuardConfig()
    symbol = symbol.upper()

    relevant = [
        e
        for e in past_executions
        if e.symbol.upper() == symbol and e.side == side and e.status in _EXECUTED_STATUSES
    ]
    if not relevant:
        return ChurnDecision(allowed=True, reason=f"no prior executed {side} of {symbol} on record")

    most_recent = max(relevant, key=lambda e: e.executed_at)

    # --- Rule 2 first (no escape hatch, so it dominates): max adds per window.
    # Buys only -- see module docstring for why exits are never capped.
    if side == "buy":
        window_start = now - timedelta(days=config.window_days)
        in_window = [e for e in relevant if e.executed_at >= window_start]
        if len(in_window) >= config.max_same_side_executions_per_window:
            return ChurnDecision(
                allowed=False,
                reason=(
                    f"{len(in_window)} executed buy(s) of {symbol} in the last {config.window_days} day(s) "
                    f"already meets the max of {config.max_same_side_executions_per_window} -- "
                    f"suppressing further adds regardless of signal strength (anti-concentration cap)"
                ),
            )

    # --- Rule 1: same-side cooldown, with the new-information escape hatch.
    age = now - most_recent.executed_at
    if age < timedelta(hours=config.cooldown_hours):
        if (
            current_blended_score is not None
            and most_recent.blended_signal_score is not None
            and abs(current_blended_score - most_recent.blended_signal_score) >= config.new_information_score_delta
        ):
            return ChurnDecision(
                allowed=True,
                reason=(
                    f"within {config.cooldown_hours:g}h cooldown of the last executed {side} of {symbol}, "
                    f"but blended score moved {most_recent.blended_signal_score:+.1f} -> "
                    f"{current_blended_score:+.1f} (>= {config.new_information_score_delta:g} pt delta) -- "
                    f"counts as new information"
                ),
            )
        return ChurnDecision(
            allowed=False,
            reason=(
                f"same-side {side} of {symbol} executed {age.total_seconds() / 3600:.1f}h ago "
                f"(cooldown {config.cooldown_hours:g}h) with no material signal change "
                f"(score {most_recent.blended_signal_score if most_recent.blended_signal_score is not None else 'n/a'} "
                f"-> {current_blended_score if current_blended_score is not None else 'n/a'}, "
                f"needs >= {config.new_information_score_delta:g} pt move) -- same information re-observed is not "
                f"new information"
            ),
        )

    return ChurnDecision(
        allowed=True,
        reason=f"last executed {side} of {symbol} was {age.total_seconds() / 3600:.1f}h ago, outside cooldown",
    )
