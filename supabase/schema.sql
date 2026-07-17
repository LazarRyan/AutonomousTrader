-- autonomous-trader schema
-- Phase 0: full table set per build plan section 7, applied to a dedicated
-- Supabase project (separate from investment-monitor). RLS is enabled on
-- every table with no anon/authenticated policies -- only the service role
-- key (used server-side by this codebase) can read/write. There is no
-- client-side/browser use of this database.

-- ============================================================
-- config: tunable trading parameters, singleton row
-- ============================================================
create table if not exists config (
    id uuid primary key default gen_random_uuid(),
    max_position_pct numeric not null default 0.05,
    max_daily_loss_pct numeric not null default 0.03,
    max_weekly_loss_pct numeric not null default 0.08,
    risk_size_weight numeric not null default 0.5,
    risk_volatility_weight numeric not null default 0.3,
    risk_liquidity_weight numeric not null default 0.2,
    risk_approval_threshold numeric not null default 70.0,
    risk_hard_override_position_pct numeric not null default 0.05,
    trading_universe text not null default 'sp500',
    updated_at timestamptz not null default now()
);

insert into config (id)
select gen_random_uuid()
where not exists (select 1 from config);

-- ============================================================
-- safety_state: singleton row, mirrors src/risk/safety_rails.py::SafetyState
-- ============================================================
create table if not exists safety_state (
    id uuid primary key default gen_random_uuid(),
    kill_switch_engaged boolean not null default false,
    daily_pnl_pct numeric not null default 0.0,
    weekly_pnl_pct numeric not null default 0.0,
    daily_halted boolean not null default false,
    weekly_halted boolean not null default false,
    last_daily_reset date,
    last_weekly_reset date,
    updated_at timestamptz not null default now()
);

insert into safety_state (id)
select gen_random_uuid()
where not exists (select 1 from safety_state);

-- ============================================================
-- holdings: current positions
-- ============================================================
create table if not exists holdings (
    id uuid primary key default gen_random_uuid(),
    symbol text not null unique,
    quantity numeric not null,
    avg_entry_price numeric not null,
    updated_at timestamptz not null default now()
);

-- ============================================================
-- signals: raw output from each signal source, per symbol per run
-- ============================================================
create table if not exists signals (
    id uuid primary key default gen_random_uuid(),
    symbol text not null,
    signal_type text not null check (signal_type in ('momentum', 'insider', 'congressional', 'news_sentiment')),
    score numeric,
    raw_data jsonb not null default '{}'::jsonb,
    generated_at timestamptz not null default now()
);

create index if not exists idx_signals_symbol_type on signals (symbol, signal_type, generated_at desc);

-- ============================================================
-- candidate_trades: proposed by the Portfolio Manager Agent, scored by the
-- risk scorer
-- ============================================================
create table if not exists candidate_trades (
    id uuid primary key default gen_random_uuid(),
    symbol text not null,
    side text not null check (side in ('buy', 'sell')),
    quantity numeric not null,
    proposed_price numeric,
    blended_signal_score numeric,
    risk_score numeric,
    risk_breakdown jsonb not null default '{}'::jsonb,
    status text not null default 'pending' check (
        status in ('pending', 'auto_approved', 'queued_for_approval', 'approved', 'rejected', 'executed', 'blocked', 'execution_failed')
    ),
    portfolio_manager_reasoning text,
    created_at timestamptz not null default now()
);

create index if not exists idx_candidate_trades_status on candidate_trades (status, created_at desc);

-- ============================================================
-- executed_trades: trades that actually went out through Alpaca (paper)
-- ============================================================
create table if not exists executed_trades (
    id uuid primary key default gen_random_uuid(),
    candidate_trade_id uuid references candidate_trades (id),
    alpaca_order_id text,
    symbol text not null,
    side text not null check (side in ('buy', 'sell')),
    quantity numeric not null,
    fill_price numeric,
    executed_at timestamptz not null default now()
);

-- ============================================================
-- approval_queue: candidate trades at/above the risk threshold, awaiting
-- Ryan's explicit y/n via scripts/review_approvals.py
-- ============================================================
create table if not exists approval_queue (
    id uuid primary key default gen_random_uuid(),
    candidate_trade_id uuid not null references candidate_trades (id),
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
    risk_score numeric,
    reasoning text,
    notified_at timestamptz,
    resolved_at timestamptz,
    resolved_by text,
    created_at timestamptz not null default now()
);

create index if not exists idx_approval_queue_status on approval_queue (status, created_at desc);

-- ============================================================
-- audit_log: every decision, taken or not, and why. Never silently dropped.
-- ============================================================
create table if not exists audit_log (
    id uuid primary key default gen_random_uuid(),
    event_type text not null,
    symbol text,
    candidate_trade_id uuid references candidate_trades (id),
    decision text not null,
    reasoning text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_audit_log_created_at on audit_log (created_at desc);

-- ============================================================
-- Row Level Security: lock every table down. Only the service role
-- (used server-side by this codebase, never exposed to a browser) bypasses
-- RLS. No anon/authenticated policies are defined anywhere on purpose.
-- ============================================================
alter table config enable row level security;
alter table safety_state enable row level security;
alter table holdings enable row level security;
alter table signals enable row level security;
alter table candidate_trades enable row level security;
alter table executed_trades enable row level security;
alter table approval_queue enable row level security;
alter table audit_log enable row level security;

-- ============================================================
-- 2026-07-16: adaptive weights + equity snapshots (applied to the live
-- project as migration adaptive_weights_and_equity_snapshots)
-- ============================================================

-- Adaptive signal blend weights (written weekly by src/signals/weight_tuner.py,
-- read by every run_cycle via BlendConfig.from_weights). NULL = equal-weight defaults.
alter table config add column if not exists signal_blend_weights jsonb;

-- Daily equity snapshots for the newsletter's performance-vs-SPY curve
-- (written by src/nightly.py once per trading day).
create table if not exists equity_snapshots (
    id uuid primary key default gen_random_uuid(),
    snapshot_date date not null unique,
    equity numeric not null,
    cash numeric not null,
    spy_close numeric,
    created_at timestamptz not null default now()
);

alter table equity_snapshots enable row level security;

-- ============================================================
-- 2026-07-16: LLM cost telemetry (applied to the live project as
-- migration llm_calls_telemetry). One row per Anthropic API call --
-- see src/llm_metering.py.
-- ============================================================
create table if not exists llm_calls (
    id uuid primary key default gen_random_uuid(),
    call_type text not null check (call_type in ('portfolio_manager', 'news_sentiment', 'reflection')),
    model text not null,
    symbol text,
    input_tokens integer not null,
    output_tokens integer not null,
    cost_usd numeric not null,
    rates_known boolean not null default true,
    created_at timestamptz not null default now()
);

create index if not exists idx_llm_calls_created_at on llm_calls (created_at desc);

alter table llm_calls enable row level security;
