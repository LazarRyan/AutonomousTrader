"""
LLM cost telemetry -- every Anthropic API call in the pipeline, metered.

Motivation (2026-07-16): the system already audits every trading DECISION,
but nothing recorded what the intelligence itself costs. Observability for
agents means being able to answer "what did today cost, and what did that
spend buy?" empirically -- cost per cycle, cost per trade, and eventually
cost per basis point of alpha (joinable against equity_snapshots). One row
per API call lands in the `llm_calls` table; the nightly newsletter's ops
section aggregates the day.

Design notes:

  - Token counts come from the API response's usage block -- ground truth,
    not estimates. Cost is computed HERE at write time from the rate table
    below and stored denormalized, so historical rows keep the price that
    was actually in effect when the call happened even if rates change
    later (rates changing under you is exactly the kind of thing that
    corrupts a cost history recomputed on read).
  - Every ATTEMPT is metered, including retries that failed to parse --
    a retried call costs real money twice, and hiding that would
    understate exactly the inefficiency this table exists to expose.
  - Writes are best-effort (same posture as db.write_signal): telemetry
    must never take down a trading cycle or the nightly job. A metering
    failure is printed and swallowed.

Split, as ever:
  1. PURE, unit-tested (tests/test_llm_metering.py): compute_cost_usd().
  2. Thin glue: write_llm_call() (one Supabase insert) and
     make_recorder() (binds a client into the record_llm_call callable
     the instrumented call sites accept via DI).
"""

from __future__ import annotations

# USD per 1M tokens, (input, output). Update alongside model changes --
# compute_cost_usd falls back to _DEFAULT_RATES (and says so via the
# `rates_known` flag) rather than refusing to meter an unknown model:
# an approximate cost row beats a missing one, but the flag keeps the
# approximation honest and queryable.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
_DEFAULT_RATES = (3.00, 15.00)


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
    """(cost_usd, rates_known) for one call. Pure, unit-tested."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be >= 0")
    rates = PRICING_PER_MTOK.get(model)
    known = rates is not None
    input_rate, output_rate = rates if known else _DEFAULT_RATES
    cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return cost, known


def write_llm_call(
    supabase_client,
    call_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    symbol: str | None = None,
) -> None:
    """Append one llm_calls row. Best-effort -- see module docstring."""
    try:
        cost_usd, rates_known = compute_cost_usd(model, input_tokens, output_tokens)
        supabase_client.table("llm_calls").insert(
            {
                "call_type": call_type,
                "model": model,
                "symbol": symbol,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "rates_known": rates_known,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 -- telemetry must never block the pipeline
        print(f"llm_calls telemetry write failed (non-blocking, {call_type}): {exc}")


def make_recorder(supabase_client, call_type: str):
    """Bind a client + call type into the `record_llm_call(model, usage,
    symbol=None)` callable that instrumented call sites accept -- keeps
    those modules free of any Supabase import, same DI pattern as
    process_candidate_trade's dependencies. `usage` is the Anthropic
    response's usage object (has .input_tokens / .output_tokens)."""

    def record(model: str, usage, symbol: str | None = None) -> None:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        write_llm_call(supabase_client, call_type, model, input_tokens, output_tokens, symbol=symbol)

    return record
