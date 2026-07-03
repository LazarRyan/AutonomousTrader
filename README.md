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

## Status: real (paper) trades have executed end to end

The system has now placed real paper orders through the full pipeline, not
just decided "nothing to propose." A wider dry run
(`scripts/dry_run.py --universe-size 30`) produced 6 candidate trades from
real signal data; 3 auto-executed (real Alpaca paper orders: A, LNT, ABT)
and 3 were correctly routed to the approval queue for exceeding the
risk-scorer's 5% position-size trigger. Every decision along the way --
proposed, scored, executed or queued, approved or blocked -- has its own
`audit_log` row with real reasoning.

A live dashboard now exists for checking on the system without writing a
new script each time -- see "Live dashboard" below.

Getting to real executions surfaced several more real bugs, on top of the
ones from the first clean dry run (Alpaca's SIP-feed subscription
restriction, an alpaca-py response-shape mismatch, a Form 4 URL that
serves rendered HTML instead of raw XML, an LLM invalid-JSON-escape edge
case):

- **Wrong Alpaca client for quotes.** `run_cycle()` was calling
  `get_latest_quote` on `TradingClient` (the order-placement client),
  which has no such method -- quotes come from the separate
  `StockHistoricalDataClient`. Every one of the first wide run's 7
  proposals failed here before this was fixed.
- **Portfolio manager leading prose + placeholder proposals.** On a
  30-symbol cycle (more to reason about than the 5-symbol runs), the model
  prefaced its JSON array with a sentence of reasoning, and separately
  included a spurious zero-quantity placeholder for a symbol it couldn't
  short. The first bug needed leading-prose stripping (same fix as the
  news-sentiment response parser); the second needed per-item
  skip-and-flag validation instead of the previous all-or-nothing
  behavior, so one bad entry no longer discards every good proposal in the
  same response.
- **The "disable thinking" truncation fix wasn't the whole story.** After
  disabling Claude Sonnet 5's adaptive thinking fixed two rounds of
  response truncation, a *third* truncation-shaped failure (MMM) happened
  anyway. Rather than guess again, `scripts/debug_sentiment_truncation.py`
  was built to pull the real API response's `stop_reason` and token
  `usage` directly -- which showed `stop_reason: end_turn`,
  `thinking_tokens: 0`, and well under the token budget: a complete, valid
  response using the identical code path. That ruled out budget/thinking
  as the cause entirely -- this is a rare, non-deterministic model
  formatting slip (it stops right after closing the `"reasoning"` string,
  before the final `}`), not a systemic bug. Fixed with a bounded retry
  (`max_attempts=2`) in both `score_news_sentiment` and
  `propose_candidate_trades`, which costs nothing extra when the first
  attempt is fine.
- **The risk-scorer's approval trigger and the safety rail's hard cap were
  the same number.** `risk/scorer.py`'s `hard_override_position_pct` (5%,
  routes to the approval queue) and `risk/safety_rails.py`'s
  `max_position_pct` (5%, the absolute execution-time cap that "cannot be
  bypassed by upstream approval") used to both default to 0.05. That meant
  any trade routed to approval *for exceeding 5%* was mathematically
  guaranteed to be blocked at execution no matter what a human approved --
  confirmed live when ABBV/AEP/ALGN were all approved via
  `scripts/review_approvals.py`, then all immediately blocked. Fixed by
  widening the safety rail's default to 15%, leaving a real 5%-15% band
  where a human's approval can actually result in a trade firing; anything
  above 15% remains an absolute block regardless of approval.

Every one of these is covered by a regression test built from the real
failure -- current count: **200 tests passing**, plus 6 more gated behind
a real `ANTHROPIC_API_KEY` (all 6 have been confirmed passing against the
real Anthropic API; they only "fail" in a sandboxed environment that can't
reach api.anthropic.com over the network, which is expected there and not
a real problem).

## Live dashboard

An artifact ("autonomous-trader-dashboard") shows live system state without
running a script each time: safety state (kill switch / daily / weekly
halts), pending approvals waiting on you with full reasoning, recent
candidate trades, executed paper trades, current holdings, and the recent
audit log. It queries the `autonomous-trader` Supabase project directly via
the already-connected Supabase MCP connector -- no separate MCP server was
needed for this. Reload it any time to get fresh data.

## What's built

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
  skip-and-flag discipline. Both parsers validated against real, live-
  fetched filings (House: Rep. Pelosi's PTR #20033725, 17/18 correct;
  Senate: Sen. Katie Britt's PTR, 21/22 correct, the remainder correctly
  flagged rather than mis-parsed in both cases).
- `src/signals/news_sentiment.py` -- Anthropic-based sentiment scoring,
  with a bounded retry for the rare non-deterministic formatting slip
  described above. Prompt/parsing logic fully tested, plus mocked-client
  tests for the retry control flow.
- `src/agents/portfolio_manager.py` -- blended-signal scoring (tested) +
  LLM trade proposals, with the same retry, leading-prose stripping, and
  per-item skip-and-flag validation described above.
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
- Diagnostic scripts: `scripts/dry_run.py` (manual one-shot cycle run, with
  `--universe-size N` for a wider one-off sample), `scripts/
  inspect_last_cycle.py` (check `audit_log`/`candidate_trades`/
  `approval_queue` from the terminal), `scripts/debug_form4.py` and
  `scripts/debug_sentiment_truncation.py` (one-off diagnostics built for
  the two real bugs described above, kept around in case either recurs).

**Credentials:** `.env` has real Supabase, Anthropic, and Alpaca (paper)
API-key values in place, and all three have been verified working from
your own machine.

## Not yet done

- **Holdings aren't synced.** `record_executed_trade()` writes to
  `executed_trades` but nothing currently updates the `holdings` table, so
  it still shows 0 rows despite 3 real executed trades. This means the
  next cycle's portfolio context (what the portfolio manager is told you
  currently own) won't reflect real positions yet -- worth fixing before
  relying on sell-side proposals or position-aware sizing.
- **Credential rotation** (Supabase, Anthropic, Alpaca all passed through
  this chat transcript during initial setup) still hasn't happened.
- This sandboxed development environment's network is allowlisted in a way
  that blocks direct calls to Supabase, Anthropic, and Alpaca -- so all
  live connectivity testing happens on your machine, not this one.
- `run_cycle()`'s config values (risk weights, thresholds, safety limits)
  live as hardcoded dataclass defaults, not yet wired up to read from the
  `config` table in Supabase, even though that table exists and
  `src/config.py`'s docstring describes it as the intended mechanism for
  tuning these without redeploying.

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
- Max position size per trade (default **15%** of portfolio -- deliberately
  higher than the risk scorer's 5% approval trigger above, so there's a
  real band where a human's approval can actually result in a trade; see
  "Status" above for the real bug this fixes)
- Max daily loss (default 3%) -- auto-halts for the rest of the day
- Max weekly loss (default 8%) -- halts until manually reset

See `src/risk/safety_rails.py` and `tests/test_safety_rails.py`.
