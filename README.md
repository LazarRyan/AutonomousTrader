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

## Status: end-to-end pipeline built, not yet run live

Every stage of the architecture (signals -> blending -> portfolio manager ->
risk scoring -> execution/approval -> audit log) is implemented and covered
by unit tests (147 passing, 6 more gated behind a real `ANTHROPIC_API_KEY`).
What's built:

- Supabase schema (`supabase/schema.sql`): `holdings`, `signals`,
  `candidate_trades`, `executed_trades`, `approval_queue`, `audit_log`,
  `safety_state`, `config`. Applied to a dedicated Supabase project.
- `src/risk/scorer.py` + `src/risk/safety_rails.py` -- deterministic
  composite risk score and non-negotiable safety rails. Fully tested.
- `src/signals/momentum.py` -- deterministic SMA crossover + RSI(14) + 10d
  ROC. Fully tested with synthetic price series.
- `src/signals/insider_edgar.py` -- SEC EDGAR Form 4 parsing + weighted
  composite score. Fully tested with fixture XML.
- `src/signals/congressional.py` -- House/Senate PTR parsing with
  skip-and-flag discipline. **Parsing regexes are not yet validated against
  a live-extracted PDF** -- see the calibration note in the module
  docstring before relying on this signal.
- `src/signals/news_sentiment.py` -- Anthropic-based sentiment scoring.
  Prompt/parsing logic tested; a fixture-headline direction test exists but
  is skipped without a real API key.
- `src/agents/portfolio_manager.py` -- blended-signal scoring (tested) +
  LLM trade proposals (fixture-scenario test gated on API key).
- `src/agents/execution.py` -- the only module that places orders. Trading-
  mode gate, safety-rail gate, and order placement all have full
  control-flow test coverage via dependency injection.
- `scripts/review_approvals.py` -- the CLI approval watcher, meant to run
  continuously. Display formatting and approval-decision handling are
  fully tested via fakes.
- `src/main.py` -- the scheduled run loop tying it all together.
  `is_trading_halted` and `process_candidate_trade` (the auto-execute vs.
  queue-for-approval decision) are fully tested; the signal-gathering loop
  itself is thin glue over the tested pieces, same as every other
  network-touching module in this project.

**Not yet done before this can run for real:**
- Populate real Alpaca paper-trading keys (trading toolset) and a real
  `ANTHROPIC_API_KEY` in `.env`.
- Replace `DEFAULT_EXAMPLE_UNIVERSE` in `src/main.py` (5 tickers) with the
  real S&P 500 constituent list -- sourcing and maintaining that list is
  its own task, not done yet.
- Validate the congressional PTR parser against real filings.
- Real 30-day volatility and liquidity metrics feed the risk scorer with
  neutral placeholders right now (see `run_cycle` in `src/main.py`) --
  needs real historical-bar-based calculations before risk scores mean
  anything.
- Nothing has been run against a live paper account yet. Every "thin
  wrapper" function that talks to Alpaca/EDGAR/House/Senate/Anthropic is
  implemented but untested against the live network from this environment.

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
