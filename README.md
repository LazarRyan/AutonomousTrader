# autonomous-trader

Autonomous, **self-learning paper-trading** system. It blends a
deterministic momentum signal with "public investment movement" signals
(congressional trading disclosures, SEC Form 4 insider filings, news
sentiment) across every active exchange-listed US equity, proposes trades
via an LLM portfolio manager that reads its own persistent memory, and
executes with full autonomy inside a layer of deterministic discipline:
anti-churn guard, sector concentration cap, liquidity floor,
regime/volatility-scaled sizing, and non-negotiable safety rails.

Every decision — taken or not — lands in an audit log with its reasoning.
Every LLM call is cost-metered. Every cycle's injected context is
persisted verbatim. The agent's memory is a folder of plain markdown you
can open directly in Obsidian.

**Paper trading only.** There is no live-trading code path; `paper` is the
default and only supported mode.

## How it learns

The trading loop runs 3x/day; a reflection loop runs nightly. Memory flows
between them through the vault:

- **Memory vault** (`vault/`, `src/memory/vault.py`) — Obsidian-compatible
  markdown: `Journal/` (one note per day, one section per cycle, including
  deliberate no-trade cycles), `Positions/` (one note per symbol: thesis,
  exit targets, full action history), `Lessons.md` (distilled rules, capped
  at 30), `Scorecard.md` (per-signal-source accuracy), `Newsletters/`.
  Wiki-links (`[[SYM]]`, `[[date]]`) make it a navigable graph.
- **Recall** (`src/memory/recall.py`) — every cycle, the portfolio manager
  prompt carries the agent's recent actions, each holding's thesis and
  live unrealized P&L, the lessons, and the signal scorecard. Crossed
  thesis exit targets are flagged explicitly in the prompt and audit log.
- **Nightly reflection** (`src/memory/reflection.py`) — FIFO-matches closed
  lots against the exact reasoning that opened and closed them, computes
  realized P&L deterministically (the LLM only distills, never does the
  math), writes lessons from realized outcomes only, and refreshes every
  open position's thesis with a required stop price and optional profit
  target.
- **Adaptive blend weights** (`src/signals/weight_tuner.py`) — weekly, each
  signal source's directional hit rate against 5-day forward returns
  becomes the next week's blend weights, with a 10% floor per source so no
  source can be permanently written off. Evidence is published to the
  scorecard.
- **Anti-churn guard** (`src/risk/churn_guard.py`) — the deterministic
  backstop for what the prompt only asks: a same-side cooldown unless the
  blended score materially moved (new information), and a hard cap on adds
  per symbol per window. Sells are never capped — exits must never be
  structurally hard.

First observable result: the day after memory shipped, the first
memory-informed cycle scanned 59 symbols and proposed nothing — where the
previous day's stateless cycles had bought the same strong signals five
separate times.

## Pipeline

```
signals (momentum / insider / congressional / news sentiment)
  -> blended score (adaptive weights)
  -> recall (memory + fundamentals + earnings calendar + market regime)
  -> LLM portfolio manager (proposals with reasoning)
  -> churn guard -> regime & volatility sizing -> sector cap -> liquidity floor
  -> deterministic risk score (telemetry)
  -> execution agent + safety rails
  -> audit log, vault journal, cost metering
```

Nothing places an order without passing every deterministic gate; nothing
at all happens without an `audit_log` row.

## Discipline layer

Applied deterministically, regardless of what the model proposes:

- **Safety rails** (`src/risk/safety_rails.py`): kill switch, max 15% of
  portfolio per trade, max daily loss 3% (auto-halts for the day), max
  weekly loss 8% (halts until manual reset), and a no-margin rail — a buy
  exceeding available cash is refused.
- **Sector cap** (`src/risk/sizing.py`): a buy that would push one sector
  past 30% of the portfolio is blocked.
- **Liquidity floor** (`src/risk/market_data.py`): buys require price ≥ $3
  and ≥ $5M average daily dollar volume — below that, spreads and data
  quality make every upstream signal unreliable.
- **Regime & volatility sizing** (`src/risk/regime.py`, `sizing.py`): buys
  are halved in a risk-off regime (SPY below its 50-day SMA with elevated
  VIX) and scaled down in names running hotter than 2x benchmark
  volatility. Sells are never scaled.
- **Risk scoring** (`src/risk/scorer.py`): a composite score
  (0.5·size + 0.3·volatility + 0.2·liquidity) is computed and persisted on
  every trade. Routing to a human approval queue is off by default
  (`require_human_approval=False`) — the system runs fully autonomously —
  but one flag restores the human-in-the-loop mode, and the approval CLI
  (`scripts/review_approvals.py`) still works.

## Universe

Built fresh each cycle: current Alpaca positions, plus tickers appearing
in a broad news pull or House PTR filings since the last cycle, ranked by
mention count and capped. Eligible symbols are any active, tradable,
exchange-listed US equity (~4-5k names from Alpaca's asset master; OTC and
units/warrants excluded). A bundled S&P 500 list is the fallback if the
asset-master fetch fails; the insider signal merges SEC's full ticker→CIK
map once per cycle.

## Scheduling

Three trading cycles on weekdays — 10:00am, 12:45pm, 3:30pm ET — via
`launchd` (`scripts/launchd/`), deliberately after the open and before the
closing auction so risk math runs on real, settled quotes. A nightly job
at 5:30pm runs the equity snapshot, reflection, weekly weight retune
(Mondays), and the newsletter. Weekends, holidays, and early closes are
detected via the exchange calendar and skipped cleanly with an audit row.

## Newsletter & observability

The nightly job emails an HTML daily letter (with a markdown copy in the
vault): performance vs. SPY from daily equity snapshots, every trade with
its reasoning, lessons (new tonight + standing), market regime, the signal
scorecard, and an ops section:

- **Cost telemetry** (`llm_calls`): every Anthropic call metered — tokens,
  dollars, retries included — broken down by pipeline stage.
- **Context snapshots** (`context_snapshots`): the exact memory/market
  context injected into each cycle's prompt, persisted before the model
  sees it. "What did the agent know when it decided X" is a queryable
  fact.
- **Citation verification**: when a proposal's reasoning cites a lesson,
  the claim is checked deterministically against the vault; fabricated
  citations are flagged in the audit log and the letter.
- **Discipline reporting**: churn suppressions, sector/liquidity blocks,
  and sizing scale-downs, per day.

## What's tested

429 tests. Every pure component — signal math, blending, risk scoring,
safety rails, churn guard, sizing, regime classification, FIFO lot
matching, prompt construction, response parsing, vault rendering/parsing,
weight tuning, newsletter rendering, cost math — is unit-tested with
synthetic inputs and no network. Network calls are thin, untested glue by
design, with per-source failure tolerance so one down feed never kills a
cycle.

## Setup

```bash
cp .env.example .env
# required: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ALPACA_API_KEY,
#           ALPACA_SECRET_KEY, ANTHROPIC_API_KEY, SEC_EDGAR_USER_AGENT
# optional (newsletter email): GMAIL_ADDRESS, GMAIL_APP_PASSWORD
pip install -e ".[dev]"
pytest
```

Then apply `supabase/schema.sql` to a Supabase project, and see
`SETUP-LEARNING.md` for the learning-system setup (yfinance, Gmail app
password, the two `launchd` jobs, opening `vault/` in Obsidian).
Useful scripts: `scripts/dry_run.py` (one-shot manual cycle),
`scripts/inspect_last_cycle.py` (terminal view of recent decisions).

## Roadmap

Deliberately sequenced behind accumulating history:

- **Vault query API + MCP server** (~2-3 months of history): parameterized
  retrieval over the vault graph (date ranges, link depth), used hybrid —
  push the non-negotiables into every prompt, pull the long tail on
  demand — once the vault outgrows push-everything.
- **LLM-as-judge evals** (once the first lessons exist): sampled nightly
  scoring of context relevance and groundedness, layered on top of the
  free deterministic checks, with judge cost itself metered in
  `llm_calls`.
- **Outcome correlation** (~30+ closed round trips): do lesson-citing
  decisions outperform non-citing ones — retrieval quality measured
  against realized P&L, not just proxy metrics.
- Senate PTR discovery (House is wired in; the Senate listing's row shape
  needs live validation first), and syncing the Supabase `holdings` table
  (trading already reads real positions from Alpaca directly).
