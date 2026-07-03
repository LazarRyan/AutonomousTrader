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

## Status: end-to-end pipeline built and verified, not yet run live

Every stage of the architecture (signals -> blending -> portfolio manager ->
risk scoring -> execution/approval -> audit log) is implemented and covered
by unit tests: 180 passing, plus 6 more gated behind a real
`ANTHROPIC_API_KEY` -- all 6 have now been confirmed passing against the
real Anthropic API (see "Credentials" below). What's built:

- Supabase schema (`supabase/schema.sql`): `holdings`, `signals`,
  `candidate_trades`, `executed_trades`, `approval_queue`, `audit_log`,
  `safety_state`, `config`. Applied to a dedicated Supabase project.
- `src/risk/scorer.py` + `src/risk/safety_rails.py` -- deterministic
  composite risk score and non-negotiable safety rails. Fully tested.
- `src/risk/market_data.py` -- real 30-day realized volatility and a
  liquidity penalty from average daily dollar volume, computed from actual
  Alpaca historical bars. Fully tested with synthetic price/volume series;
  feeds the risk scorer in place of the earlier neutral placeholders.
- `src/universe.py` + `data/sp500_constituents.csv` -- the real S&P 500
  constituent list (with SEC CIKs bundled for the insider signal), sourced
  from a maintained public dataset. `src/main.py` defaults to this list.
  Refresh periodically with `scripts/refresh_sp500_universe.py`.
- `src/signals/momentum.py` -- deterministic SMA crossover + RSI(14) + 10d
  ROC. Fully tested with synthetic price series.
- `src/signals/insider_edgar.py` -- SEC EDGAR Form 4 parsing + weighted
  composite score. Fully tested with fixture XML.
- `src/signals/congressional.py` -- House/Senate PTR parsing with
  skip-and-flag discipline. **Both parsers validated against real, live-
  fetched filings.** House: Rep. Pelosi's PTR #20033725, 17 of 18 real
  transactions parsed correctly, the 18th (a row mangled by a PDF page
  break) correctly flagged rather than mis-parsed. Senate: Sen. Katie
  Britt's PTR filed 01/26/2026 (fetched via a public PDF mirror, since
  efdsearch.senate.gov's interactive terms-acceptance session couldn't be
  completed from this environment), 21 of 22 real transactions parsed
  correctly, the 22nd (page-break-scrambled) correctly flagged. The
  Senate parser's original single-line-per-row layout assumption turned
  out to be wrong once tested against real data (real rows wrap across
  multiple lines, same as House) and was reworked accordingly -- a genuine
  bug caught by validation, not a hypothetical.
- `src/signals/news_sentiment.py` -- Anthropic-based sentiment scoring.
  Prompt/parsing logic tested; the fixture-headline direction test (gated
  on `ANTHROPIC_API_KEY`) has been run against the real API and passes.
  Fixed a real bug found by that run: the response's first content block
  can be a `ThinkingBlock` rather than text, and the model can append
  stray commentary after the JSON payload -- both now handled.
- `src/agents/portfolio_manager.py` -- blended-signal scoring (tested) +
  LLM trade proposals. The fixture-scenario test (gated on
  `ANTHROPIC_API_KEY`) has been run against the real API and passes, after
  the same `ThinkingBlock`/trailing-JSON fixes as news_sentiment.py.
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

**Credentials:** `.env` has real Supabase, Anthropic, and Alpaca (paper)
API-key values in place, and all three have been **verified working from
your own machine** -- Supabase query returned real data, Alpaca account
check showed `AccountStatus.ACTIVE`, Anthropic call returned `OK`, and the
6 previously-gated fixture tests pass against the real Anthropic API.
These secrets passed through this chat session's transcript at some point
during setup -- still worth rotating all three at some point, since a chat
log isn't a secure long-term home for live credentials, but nothing is
blocking on it.

**Not yet done before this can run for real:**
- Nothing has been run against a live paper account yet -- the first real
  `run_cycle()` against your actual paper account and the full S&P 500
  universe is the natural next milestone.
- This sandboxed development environment's network is allowlisted in a way
  that blocks direct calls to Supabase, Anthropic, and Alpaca -- so all
  connectivity testing above happened on your machine, not this one.

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
