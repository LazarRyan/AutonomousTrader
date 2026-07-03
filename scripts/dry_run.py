"""
One-shot MANUAL dry run of src.main.run_cycle() against your REAL Supabase
project and REAL Alpaca PAPER account -- this is the first genuine
end-to-end test of the whole pipeline, deliberately scoped to a small,
explicit universe rather than the full S&P 500.

Run with: python scripts/dry_run.py

Why a small universe first: run_cycle()'s default (src.universe.load_sp500_universe())
is ~500 symbols. Scanning all of them means ~500 sequential Alpaca momentum
fetches, ~500 SEC EDGAR insider lookups, ~500 Alpaca news fetches, and up to
~500 real Anthropic sentiment calls before the portfolio manager even runs
-- slow, and a real (if small) API cost, on a FIRST run before you've seen
what a single cycle actually looks like end to end. DRY_RUN_UNIVERSE below
mirrors src.main.DEFAULT_EXAMPLE_UNIVERSE (5 large, liquid, high-news-volume
names) to keep the first run fast and cheap to eyeball. Widen it yourself
(or just call run_cycle with universe=None / omit the override) once you've
looked at one cycle's results and are comfortable with what it does.

What it does, in order (identical pipeline to the real scheduled loop in
src.main.run_cycle -- this script is not a simulation, it calls the exact
same function):
  1. Checks the kill switch / daily / weekly halt state in Supabase --
     bails immediately if trading is halted, same as the real loop would.
  2. Fetches momentum / insider / news-sentiment signals for each symbol in
     DRY_RUN_UNIVERSE (congressional is aggregated at the run_cycle level
     across many filers' recent filings and will be neutral/absent for an
     ad-hoc single-cycle run like this -- that aggregation isn't wired into
     run_cycle yet, see the congressional signal's own module for why).
  3. Sends the blended per-symbol scores plus your REAL paper account's
     current cash/holdings to the Portfolio Manager Agent (a real Anthropic
     API call) and gets back zero or more candidate trades.
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

import os
import sys

sys.path.insert(0, ".")  # allow `python scripts/dry_run.py` from repo root

# Small, explicit universe for a first manual dry run -- NOT the real S&P
# 500. Mirrors src.main.DEFAULT_EXAMPLE_UNIVERSE. Widen this list (or pass
# your own) once you've reviewed one cycle's results.
DRY_RUN_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]


def main() -> None:
    from alpaca.trading.client import TradingClient

    from src.config import load_settings
    from src.db import get_client
    from src.main import run_cycle

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

    supabase_client = get_client(settings)
    alpaca_trading_client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)

    account = alpaca_trading_client.get_account()
    print("=" * 70)
    print("AUTONOMOUS-TRADER -- MANUAL DRY RUN")
    print("=" * 70)
    print(f"Paper account status : {account.status}")
    print(f"Paper account equity : ${float(account.equity):,.2f}")
    print(f"Paper account cash   : ${float(account.cash):,.2f}")
    print(f"Universe (this run)  : {', '.join(DRY_RUN_UNIVERSE)}")
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
    run_cycle(supabase_client, alpaca_trading_client, settings, sec_user_agent, universe=DRY_RUN_UNIVERSE)

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
