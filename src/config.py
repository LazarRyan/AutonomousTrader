"""
Central config loading. Two layers:

1. Environment variables (.env) -- secrets and deployment-level settings that
   should never live in the DB: Supabase URL/key, Alpaca keys, TRADING_MODE.
2. The `config` table in Supabase -- tunable trading parameters that Ryan may
   want to change without redeploying: risk weights, thresholds, safety rail
   limits. Loaded at the start of each run cycle, never cached indefinitely.

TRADING_MODE is the most safety-critical value here. Paper is the only
supported mode until live trading is explicitly built as its own deliberate
phase (see build plan section 2). This module refuses to treat anything
other than the literal string "live" as a request for live trading, and even
then live trading requires a separate manual confirmation step in the
execution agent -- this module does not grant it by itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    alpaca_api_key: str
    alpaca_secret_key: str
    trading_mode: str  # "paper" (default/only supported for now) or "live"
    anthropic_api_key: str | None = None  # required for the LLM-based signal/agent, not for Phase 0 core

    @property
    def is_live_mode(self) -> bool:
        return self.trading_mode == "live"


def load_settings() -> Settings:
    """Load required settings from environment. Raises ConfigError if anything
    required is missing -- fail loud at startup, not silently mid-cycle."""
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
        "ALPACA_API_KEY": os.getenv("ALPACA_API_KEY"),
        "ALPACA_SECRET_KEY": os.getenv("ALPACA_SECRET_KEY"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")

    trading_mode = os.getenv("TRADING_MODE", "paper")
    if trading_mode not in ("paper", "live"):
        raise ConfigError(f"TRADING_MODE must be 'paper' or 'live', got {trading_mode!r}")

    return Settings(
        supabase_url=required["SUPABASE_URL"],
        supabase_key=required["SUPABASE_SERVICE_ROLE_KEY"],
        alpaca_api_key=required["ALPACA_API_KEY"],
        alpaca_secret_key=required["ALPACA_SECRET_KEY"],
        trading_mode=trading_mode,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    )
