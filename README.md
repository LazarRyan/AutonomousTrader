# autonomous-trader

Autonomous, **self-learning** **paper-trading** system. Blends a
deterministic momentum signal with "public investment movement" following
(congressional/insider disclosures + news sentiment) across every active
exchange-listed US equity, proposes trades via an LLM portfolio manager
that reads its own persistent memory (an Obsidian-compatible markdown
vault of journals, per-position theses, distilled lessons, and a signal
scorecard), and executes with **full autonomy** -- no human approval gate
since 2026-07-16. Discipline is deterministic, not hoped for: an
anti-churn guard, sector concentration cap, liquidity floor,
regime/volatility-scaled sizing, and non-negotiable safety rails (position
size, daily/weekly loss halts, kill switch, no-margin, full audit log)
apply regardless of what the model wants.

A nightly job reflects on realized outcomes (FIFO-matched closed lots vs.
the exact reasoning that opened them), writes lessons and per-position
theses with concrete exit targets back into the vault, retunes the signal
blend weights weekly from each source's measured predictive accuracy, and
emails a daily HTML letter (performance vs. SPY, every trade with
reasoning, lessons, ops metrics). Every LLM call is cost-metered
(`llm_calls`), every cycle's injected context is persisted verbatim
(`context_snapshots`), and lesson citations in trade reasoning are
deterministically verified against the vault -- see "The learning system"
below.

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
failure -- current count: **229 tests passing**, plus 6 more gated behind
a real `ANTHROPIC_API_KEY` (all 6 have been confirmed passing against the
real Anthropic API; they only "fail" in a sandboxed environment that can't
reach api.anthropic.com over the network, which is expected there and not
a real problem).

## The learning system (2026-07-16)

The stateless-agent era ended when the audit trail showed five separate
KHC buys across three days, each cycle independently rediscovering the
same signal with no memory of having acted on it. The learning upgrade,
in the order information flows:

- **Memory vault** (`vault/`, `src/memory/vault.py`) -- plain markdown,
  openable directly as an Obsidian vault: `Journal/` (one note per day,
  one section per cycle -- including no-trade cycles), `Positions/` (one
  note per symbol: thesis + exit targets + action history), `Lessons.md`
  (capped at 30, newest first), `Scorecard.md`, `Newsletters/`.
  Wiki-links (`[[SYM]]` in journals, `[[date]]` in position histories)
  make it a navigable graph.
- **Recall** (`src/memory/recall.py`) -- every cycle, the portfolio
  manager prompt carries the agent's own recent actions, each holding's
  thesis, the lessons, and the signal scorecard. Holdings include live
  prices + unrealized P&L, and crossed thesis exit targets are flagged
  loudly (`thesis_target`/`target_crossed` audit rows). First observable
  effect: the first memory-informed cycle (2026-07-17 10:00) scanned 59
  symbols and proposed nothing -- restraint, where the day before it had
  bought the same signals five times.
- **Anti-churn guard** (`src/risk/churn_guard.py`) -- deterministic
  backstop for what the prompt only asks: same-side cooldown (20h) unless
  the blended score moved >= 15 points (new information), max 2 buys per
  symbol per 5 days. Sells are never capped -- exits must never be
  structurally hard.
- **Nightly reflection** (`src/memory/reflection.py`, 5:30pm via
  `launchd`) -- FIFO-matches closed lots against the exact entry/exit
  reasoning, computes realized P&L deterministically (the LLM only
  distills, never computes), writes lessons (realized outcomes only,
  never unrealized hunches), and refreshes every open position's thesis
  with a required stop (`exit_below`) and optional profit target
  (`exit_above`).
- **Adaptive blend weights** (`src/signals/weight_tuner.py`, Mondays) --
  each source's directional hit rate vs. 5-day forward returns over the
  trailing 30 days becomes next week's blend weights (10% floor per
  source so no source can be permanently condemned; all-noise reverts to
  equal weights). Evidence lands in `Scorecard.md`.
- **Market context** (`src/signals/fundamentals.py`, `src/risk/regime.py`,
  `src/risk/sizing.py`) -- yfinance fundamentals + earnings-date warnings
  in the prompt; SPY-SMA/VIX regime read that deterministically halves
  buy sizes in risk_off; volatility-normalized sizing; 30% sector cap;
  $3-price/$5M-dollar-volume liquidity floor for buys.
- **Nightly newsletter** (`src/newsletter.py`) -- HTML email (Gmail SMTP,
  markdown fallback + vault copy): performance vs. SPY from daily
  `equity_snapshots`, every trade with reasoning, lessons (new tonight +
  standing), regime, scorecard, and ops metrics.
- **Observability** -- `llm_calls` meters every Anthropic call (tokens +
  cost, retries included); `context_snapshots` persists the exact
  memory/market context injected per cycle ("what did the agent know when
  it decided X" is a queryable fact); lesson citations in reasoning are
  deterministically verified against the vault, with fabrications flagged
  in `audit_log` and the newsletter.

One-time setup for all of this: `SETUP-LEARNING.md`.

## Scheduling: 3x/day instead of continuous, with a redesigned universe

`run_cycle()` is now meant to run on a fixed schedule (10:00am / 12:45pm /
3:30pm ET, via `launchd` -- see `scripts/launchd/README.md` for setup;
re-timed 2026-07-16 from 9:20/1:30/3:50 -- the pre-open slot priced risk
on thin pre-market quotes and filled in the opening auction) rather than
continuously, and its default trading universe changed to match: instead
of blindly scanning a static index list every cycle, it's built fresh
each run from real current Alpaca positions (`get_all_positions()`, not
the unsynced `holdings` table -- see below) plus whatever tickers
actually showed up in a broad news pull or in House PTR filings since the
last cycle, ranked by mention count and capped at 50. Discovery is
filtered to every active, tradable, exchange-listed US equity (~4-5k
names from Alpaca's asset master; OTC and units/warrants excluded --
`fetch_tradable_universe` in `src/universe.py`), no longer the S&P 500
alone; the bundled S&P CSV remains the fallback and the insider signal
merges SEC's full ticker->CIK map once per cycle. `src/discovery.py` holds the pure, tested ranking/lookback-
window logic; the network glue that feeds it lives next to each source's
other wrappers in `news_sentiment.py` and `congressional.py`.

This also finally wires the congressional signal into `run_cycle()` --
previously always `None`. House discovery is fully wired (using the
already-validated `fetch_house_disclosure_index`); Senate discovery is
deliberately NOT wired in yet, since `fetch_senate_ptr_listing`'s real row
shape has never been validated against the live site -- run
`scripts/debug_senate_listing.py` once to get real evidence before wiring
it in, rather than guessing at field names.

A market-day check (via Alpaca's real `get_clock`/`get_calendar`) was
added at the top of `run_cycle()` so a scheduled run firing on a weekend,
holiday, or after an early close skips cleanly with an `audit_log` row
instead of running against a closed market. An explicit `universe`
argument (as `scripts/dry_run.py` always passes) bypasses all of this and
uses exactly the list given, same as before.

Apache Airflow was considered and deliberately not used for the
scheduling itself -- there's no multi-task orchestration need here (one
script, no internal dependencies), and Airflow would still need
`launchd`/`brew services` underneath to keep its own scheduler alive, so
it would add a database and a second system to maintain without removing
the actual OS-level "keep this running" requirement.

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
- `src/discovery.py` -- pure, fully-tested per-cycle universe discovery:
  `rank_discovered_symbols()` (mention-frequency ranking + cap + universe
  filter) and `compute_lookback_start()` (schedule-aware lookback window,
  see "Scheduling" above).
- `scripts/launchd/` -- the `launchd` setup for the 3x/day schedule: a
  `.plist`, a wrapper script, and setup/troubleshooting instructions.
- Diagnostic scripts: `scripts/dry_run.py` (manual one-shot cycle run, with
  `--universe-size N` for a wider one-off sample), `scripts/
  inspect_last_cycle.py` (check `audit_log`/`candidate_trades`/
  `approval_queue` from the terminal), `scripts/debug_form4.py`,
  `scripts/debug_sentiment_truncation.py`, and
  `scripts/debug_senate_listing.py` (one-off diagnostics, kept around in
  case any of the underlying issues recur).
- The full learning/observability layer (2026-07-16/17) -- see "The
  learning system" above: `src/memory/{vault,recall,reflection}.py`,
  `src/risk/{churn_guard,regime,sizing}.py`,
  `src/signals/{fundamentals,weight_tuner}.py`, `src/newsletter.py`,
  `src/nightly.py`, `src/llm_metering.py`, plus the `equity_snapshots`,
  `llm_calls`, and `context_snapshots` tables and the nightly `launchd`
  job (`scripts/launchd/com.ryan.autonomous-trader.nightly.plist`). All
  pure logic fully tested -- the suite is at 429 tests.

**Credentials:** `.env` has real Supabase, Anthropic, and Alpaca (paper)
API-key values in place, and all three have been verified working from
your own machine.

## Not yet done

- **The Supabase `holdings` table still isn't synced** -- `record_executed_trade()`
  writes to `executed_trades` but nothing updates `holdings`, so it still
  shows 0 rows despite real executed trades. This no longer affects
  trading decisions (`run_cycle()` now reads real positions directly from
  Alpaca's `get_all_positions()` instead -- see "Scheduling" above), but
  the live dashboard artifact still reads the Supabase table directly, so
  its "Holdings" section will keep showing stale/empty data until this is
  fixed or the dashboard is pointed at Alpaca too.
- **Senate PTR discovery isn't wired in.** House filings are; Senate isn't,
  because `fetch_senate_ptr_listing`'s real row shape has never been
  validated against the live site. Run `scripts/debug_senate_listing.py`
  once to get real evidence, then wire in a proper field mapping.
- **launchd needs your Python interpreter path filled in** before it'll
  work -- `scripts/launchd/run_cycle.sh` has a placeholder
  (`REPLACE_WITH_OUTPUT_OF_WHICH_PYTHON`) that needs the real output of
  `which python` from your conda environment. See
  `scripts/launchd/README.md` for the full one-time setup.
- **Credential rotation** (Supabase, Anthropic, Alpaca all passed through
  this chat transcript during initial setup) still hasn't happened.
- This sandboxed development environment's network is allowlisted in a way
  that blocks direct calls to Supabase, Anthropic, and Alpaca -- so all
  live connectivity testing happens on your machine, not this one.
- `run_cycle()`'s RISK config values (risk weights, thresholds, safety
  limits) live as hardcoded dataclass defaults, not yet wired up to read
  from the `config` table in Supabase. (The signal BLEND weights are the
  exception since 2026-07-16: `config.signal_blend_weights` is written by
  the weekly retune and read by every cycle.)
- **Planned next tiers** (deliberately waiting for enough history -- see
  the timeline discussion in the vault/newsletter): a parameterized vault
  query API behind an MCP server (hybrid push/pull memory once the vault
  outgrows push-everything, ~2-3 months of history), and sampled
  LLM-as-judge evals of context relevance/groundedness (worth starting
  once the first lessons exist, ~2-3 weeks; aggregates meaningful at ~1
  month).

## Setup

```bash
cp .env.example .env
# fill in SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
# optional (newsletter email): GMAIL_ADDRESS, GMAIL_APP_PASSWORD
pip install -e ".[dev]"
pytest
```

Then `SETUP-LEARNING.md` for the learning-system one-time setup (yfinance,
Gmail app password, the nightly `launchd` job, opening `vault/` in
Obsidian).

## Architecture

Signals -> recall (memory + market context) -> LLM portfolio manager ->
churn guard -> sizing (regime + volatility) -> sector cap + liquidity
floor -> risk scorer -> execution + safety rails -> audit log, with the
vault/reflection loop feeding memory back in nightly. Nothing places an
order without passing every deterministic gate; nothing at all happens
without an `audit_log` row.

## Risk scoring

```
composite_risk_score =
    0.5 * size_component        (trade_value / portfolio_value)
  + 0.3 * volatility_component  (asset_30d_vol / benchmark_30d_vol, capped)
  + 0.2 * liquidity_component   (liquidity penalty)
```

**Full autonomy (2026-07-16):** the score, the 70-point threshold
comparison, and the >5% hard override are still computed and persisted on
every trade as telemetry, but they no longer route anything to the human
approval queue -- `RiskScorerConfig.require_human_approval` defaults to
`False`. Set it `True` to restore the pre-autonomy human-in-the-loop
behavior (`scripts/review_approvals.py` still works). Weights and
thresholds remain configurable in `RiskScorerConfig`
(`src/risk/scorer.py`).

## Safety rails (non-negotiable)

Checked before every order, independent of risk-score approval status:
- Kill switch (single flag, refuses all orders when engaged)
- Max position size per trade (default **15%** of portfolio -- originally
  set above the risk scorer's 5% approval trigger so approvals had a real
  band to act in, see "Status" above for the bug that motivated it; under
  full autonomy it stands on its own as the absolute per-trade ceiling)
- Max daily loss (default 3%) -- auto-halts for the rest of the day
- Max weekly loss (default 8%) -- halts until manually reset

See `src/risk/safety_rails.py` and `tests/test_safety_rails.py`.
