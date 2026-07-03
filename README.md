# autonomous-trader

Autonomous **paper-trading** system. Blends a deterministic momentum signal
with "public investment movement" following (congressional/insider
disclosures + news sentiment), scores every candidate trade with a
deterministic composite risk score, auto-executes low-risk trades, and
queues anything at/above the risk threshold for your explicit approval.
Hard safety rails (position size, daily/weekly loss halts, kill switch,
full audit log) apply regardless of anything above.

This is a separate project from `investment-monitor`, with its own repo,
own Supabase project, and its own Alpaca paper keys (the trading toolset is
a materially more sensitive permission grant than the monitor's
market-data-only keys, even though it's fine to reuse the same Alpaca
account).

**No live trading exists in this codebase yet.** Paper is the default and
only supported mode. Live trading requires a distinct `TRADING_MODE=live`
env var *and* a manual confirmation step in the execution agent that does
not exist yet and will be built as its own deliberate second phase.

## Status: Phase 0 (scaffolding)

What exists right now:
- Repo layout, `.env.example`, `pyproject.toml`.
- Supabase schema (`supabase/schema.sql`): `holdings`, `signals`,
  `candidate_trades`, `executed_trades`, `approval_queue`, `audit_log`,
  `safety_state`, `config`.
- `src/risk/scorer.py` -- deterministic composite risk score, fully
  implemented and unit-tested.
- `src/risk/safety_rails.py` -- kill switch, max position size, daily/weekly
  loss auto-halt, fully implemented and unit-tested.
- Everything else (`src/signals/*`, `src/agents/*`, `src/main.py`,
  `scripts/review_approvals.py`) is a stub with a docstring describing what
  it will do -- deliberately not built yet, per the phase discipline in the
  build plan.

## Setup

```bash
cp .env.example .env
# fill in SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
pip install -e ".[dev]"
pytest
```

## Architecture

See the build plan doc for the full signal -> portfolio manager -> risk
scorer -> execution/approval -> audit log pipeline. In short: nothing places
an order without passing through both the risk scorer (decides if it needs
human approval) and the safety rails (a stricter, non-negotiable layer
checked before every single order regardless of approval status).

## Risk scoring

```
composite_risk_score =
    0.5 * size_component        (trade_value / portfolio_value)
  + 0.3 * volatility_component  (asset_30d_vol / benchmark_30d_vol, capped)
  + 0.2 * liquidity_component   (liquidity penalty)

needs_approval = composite_risk_score >= 70  OR  position_pct > 5%  (hard override)
```

Weights, threshold, and the hard-override percentage are all configurable
in `RiskScorerConfig` (`src/risk/scorer.py`) and intended to be tuned after
backtesting.

## Safety rails (non-negotiable)

Checked before every order, independent of risk-score approval status:
- Kill switch (single flag, refuses all orders when engaged)
- Max position size per trade (default 5% of portfolio)
- Max daily loss (default 3%) -- auto-halts for the rest of the day
- Max weekly loss (default 8%) -- halts until manually reset

See `src/risk/safety_rails.py` and `tests/test_safety_rails.py`.
