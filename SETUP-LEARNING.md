# Learning upgrade — one-time setup

Three steps on your machine, ~10 minutes total.

## 1. Install the new dependency

```bash
/opt/anaconda3/bin/pip install yfinance
```

(Used for fundamentals, earnings dates, and the VIX read. Everything degrades gracefully if a fetch fails — no trade ever blocks on Yahoo.)

## 2. Gmail App Password for the nightly newsletter

1. Go to https://myaccount.google.com/apppasswords (requires 2-Step Verification enabled on the account).
2. Create an app password named e.g. `autonomous-trader`.
3. Add to `.env`:

```
GMAIL_ADDRESS=lazar.ryan123@gmail.com
GMAIL_APP_PASSWORD=<the 16-character app password>
```

Skip this if you only want the newsletter in the vault — email is optional and the nightly job says so in its log rather than failing.

## 3. Load the nightly launchd job

```bash
chmod +x scripts/launchd/run_nightly.sh
cp scripts/launchd/com.ryan.autonomous-trader.nightly.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ryan.autonomous-trader.nightly.plist
```

It fires weekdays at 5:30pm, logs to `~/Library/Logs/autonomous-trader/nightly.log`.

To run the nightly job once by hand right now (writes the first equity snapshot, first journal reflection, and first newsletter):

```bash
cd ~/dev/AutonomousTrader && /opt/anaconda3/bin/python -m src.nightly
```

## Full autonomy + full-market universe (2026-07-16)

- **No more approval queue.** Every proposal now auto-executes; nothing waits for you. The risk score and "would have needed approval" telemetry still land in every `candidate_trades` row. To restore the human gate, pass `RiskScorerConfig(require_human_approval=True)` (or flip the default back in `src/risk/scorer.py`). `scripts/review_approvals.py` still works for anything already sitting in the queue.
- **What still acts on its own:** daily/weekly loss halts, 15% max position, no-margin rail, 30% sector cap, churn guard, and the new liquidity floor. These are the trader's own discipline, not requests for your sign-off.
- **Universe:** discovery now accepts any active, tradable, exchange-listed US equity (~4-5k names; OTC, units/warrants excluded). Hard data-quality floor on buys: price ≥ $3 and ≥ $5M average daily dollar volume. Falls back to the S&P 500 list if the Alpaca asset fetch fails.

## What's already live without any setup

- The Supabase migration (blend-weights column + equity_snapshots table) is applied.
- The next scheduled trading cycle automatically: recalls its recent actions/lessons/theses into the prompt, applies the churn guard (20h same-side cooldown unless the signal moved ≥15 pts; max 2 buys per symbol per 5 days), scales buys for market regime and volatility, and enforces the 30% sector cap.
- The first weight retune runs Monday night; until then the blend stays 25/25/25/25.

## Where to see it learning

- Open `vault/` in Obsidian — Journal fills in from the next cycle onward.
- `Lessons.md` starts populating the first night after a position is closed.
- Audit log: `churn_suppressed`, `quantity_scaled`, `sector_cap_blocked`, and `weight_retune` are the new event types.
