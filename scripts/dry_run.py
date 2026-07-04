"""
One-shot MANUAL dry run of src.main.run_cycle() against your REAL Supabase
project and REAL Alpaca PAPER account -- this is the first genuine
end-to-end test of the whole pipeline, deliberately scoped to a small,
explicit universe rather than the full S&P 500.

Run with: python scripts/dry_run.py
Or with a wider universe for one run: python scripts/dry_run.py --universe-size 30

Why a small universe first: run_cycle()'s default (src.universe.load_sp500_universe())
is ~500 symbols. Scanning all of them means ~500 sequential Alpaca momentum
fetches, ~500 SEC EDGAR insider lookups, ~500 Alpaca news fetches, and up to
~500 real Anthropic sentiment calls before the portfolio manager even runs
-- slow, and a real (if small) API cost, on a FIRST run before you've seen
what a single cycle actually looks like end to end. DRY_RUN_UNIVERSE below
mirrors src.main.DEFAULT_EXAMPLE_UNIVERSE (5 large, liquid, high-news-volume
names) to keep the first run fast and cheap to eyeball.

--universe-size N overrides DRY_RUN_UNIVERSE with the first N symbols from
the real S&P 500 list (src.universe.load_sp500_universe()) instead -- every
run against the 5-symbol default so far has produced a deliberate,
correctly-logged zero-trade decision (moderately-signed blended scores,
nothing strong enough to propose), so a wider one-off sample is a
reasonable way to actually exercise the risk-scoring/execution/
approval-queue code paths for real, without going anywhere near the full
~500-symbol universe. Each additional symbol adds real Alpaca/SEC EDGAR
calls plus one real (thinking-disabled, cheap) Anthropic sentiment call --
use a moderate size (tens, not hundreds) for a one-off check.

What it does, in order (identical pipeline to the real scheduled loop in
src.main.run_cycle -- this script is not a simulation, it calls the exact
same function):
  1. Checks the kill switch / daily / weekly halt state in Supabase, and
     (now) the real Alpaca market calendar -- bails immediately if trading
     is halted or today isn't a trading day, same as the real loop would.
  2. Fetches momentum / insider / news-sentiment signals for each symbol in
     DRY_RUN_UNIVERSE. Congressional will be neutral/absent here: House PTR
     discovery IS now wired into run_cycle, but only for its dynamic
     default universe (real holdings + news/filing discovery) -- passing an
     explicit universe, as this script always does, bypasses both news and
     congressional discovery entirely and uses exactly the list you give
     it. See run_cycle()'s docstring in src/main.py for the full design.
  3. Sends the blended per-symbol scores plus your REAL paper account's
     current cash/holdings (read directly from Alpaca's real positions, not
     the Supabase `holdings` table) to the Portfolio Manager Agent (a real
     Anthropic API call) and gets back zero or more candidate trades.
  4. For each candidate: scores its risk, writes a candidate_trades row
     (ALWAYS -- whether or not anything ends up executing), and either:
       - auto-executes it as a REAL PAPER order via Alpaca (paper money,
         not real money, but a genuine order placed against your paper
         account, not a simulation of one), or
       - queues it in approval_queue for you to review by hand with
         `python scripts/review_approvals.py`.
  5. Writes an audit_log row for every decision along the way, taken or not.

Safe-by-default checks before anything runs: refuses to proceed if
TRADING_MODE isn't "paper", and asks for an explicit y/n confirmation after
showing your real paper account's current equity/cash -- nothing fires
silently.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, ".")  # allow `python scripts/dry_run.py` from repo root

# Small, explicit universe for a first manual dry run -- NOT the real S&P
# 500. Mirrors src.main.DEFAULT_EXAMPLE_UNIVERSE. Override for one run with
# --universe-size N (see module docstring), or edit this list directly for
# a permanent change.
DRY_RUN_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--universe-size",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Use the first N symbols from the real S&P 500 list instead of the "
            "default 5-symbol DRY_RUN_UNIVERSE. Adds real API calls per symbol -- "
            "use a moderate size (tens, not hundreds) for a one-off check."
        ),
    )
    return parser.parse_args()


def main() -> None:
    from alpaca.trading.client import TradingClient

    from src.config import load_settings
    from src.db import get_client
    from src.main import run_cycle

    args = _parse_args()

    settings = load_settings()

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY must be set -- see .env.example")
    if settings.trading_mode != "paper":
        raise RuntimeError(
            f"Refusing to dry-run with TRADING_MODE={settings.trading_mode!r} -- this script only runs in 'paper' mode"
        )

    sec_user_agent = os.getenv("SEC_EDGAR_USER_AGENT")
    if not sec_user_agent:
        raise RuntimeError("SEC_EDGAR_USER_AGENT must be set -- see .env.example")

    if args.universe_size:
        from src.universe import load_sp500_universe

        full_universe = load_sp500_universe()
        universe = full_universe[: args.universe_size]
        if len(universe) < args.universe_size:
            print(
                f"Note: requested {args.universe_size} symbols but the S&P 500 "
                f"list only has {len(full_universe)}; using all of them."
            )
    else:
        universe = DRY_RUN_UNIVERSE

    supabase_client = get_client(settings)
    alpaca_trading_client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)

    account = alpaca_trading_client.get_account()
    print("=" * 70)
    print("AUTONOMOUS-TRADER -- MANUAL DRY RUN")
    print("=" * 70)
    print(f"Paper account status : {account.status}")
    print(f"Paper account equity : ${float(account.equity):,.2f}")
    print(f"Paper account cash   : ${float(account.cash):,.2f}")
    if len(universe) <= 10:
        print(f"Universe (this run)  : {', '.join(universe)}")
    else:
        print(f"Universe (this run)  : {len(universe)} symbols ({universe[0]}...{universe[-1]})")
    print()
    print("This will make real network calls (Alpaca, SEC EDGAR, Anthropic),")
    print("write real rows to your Supabase project, and MAY place a real")
    print("(paper) Alpaca order if the portfolio manager proposes a trade")
    print("that scores below the risk-approval threshold.")
    print()

    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted -- nothing was run.")
        return

    print("\nRunning cycle...\n")
    run_cycle(supabase_client, alpaca_trading_client, settings, sec_user_agent, universe=universe)

    print("\n" + "=" * 70)
    print("Cycle complete. Check results with:")
    print("  Supabase SQL editor:")
    print("    select * from candidate_trades order by created_at desc limit 10;")
    print("    select * from audit_log order by created_at desc limit 20;")
    print("    select * from approval_queue where status = 'pending';")
    print("  If anything is pending approval:")
    print("    python scripts/review_approvals.py")
    print("=" * 70)


if __name__ == "__main__":
    main()
