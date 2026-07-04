# launchd schedule: 9:40am / 2:30pm / 3:50pm ET

Runs `python -m src.main` (the real scheduled entry point -- places real
paper trades, same as everything else in this project) three times a day
instead of continuously. Weekends, holidays, and early closes are handled
by `run_cycle()`'s own market-day check (added specifically for this),
not by launchd -- see `src/main.py`'s docstring.

## One-time setup

1. **Fill in your Python interpreter path.** Open `run_cycle.sh` in this
   directory and replace `REPLACE_WITH_OUTPUT_OF_WHICH_PYTHON` with the
   output of:

   ```bash
   which python
   ```

   run from the same terminal/environment where `pytest` and `dry_run.py`
   already work (your conda `base` environment). Do not leave it as a bare
   `python` -- launchd's minimal environment doesn't have your shell's PATH
   or conda setup, so it won't resolve that on its own.

2. **Make the wrapper script executable:**

   ```bash
   chmod +x /Users/ryanlazar/Documents/AutonomousTrader/scripts/launchd/run_cycle.sh
   ```

3. **Create the log directory** (launchd won't create it for you):

   ```bash
   mkdir -p ~/Library/Logs/autonomous-trader
   ```

4. **Copy the plist into LaunchAgents:**

   ```bash
   cp /Users/ryanlazar/Documents/AutonomousTrader/scripts/launchd/com.ryan.autonomous-trader.run-cycle.plist \
      ~/Library/LaunchAgents/
   ```

5. **Load it:**

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
   ```

   (On older macOS versions where `bootstrap` isn't available, use
   `launchctl load ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist`
   instead.)

## Verifying it's loaded

```bash
launchctl list | grep autonomous-trader
```

Should print a line with the label `com.ryan.autonomous-trader.run-cycle`.

## Testing it right now, without waiting for the next scheduled time

```bash
launchctl kickstart -k gui/$(id -u)/com.ryan.autonomous-trader.run-cycle
```

Then check the logs:

```bash
tail -f ~/Library/Logs/autonomous-trader/run-cycle.log
tail -f ~/Library/Logs/autonomous-trader/run-cycle.err.log
```

This is a REAL run against your real paper account, Supabase project, and
Anthropic API -- same as running `python -m src.main` by hand. Make sure
you're comfortable with that before kickstarting it (or before the next
scheduled time arrives).

## Making changes later

If you edit `run_cycle.sh` or the `.plist` after it's already loaded,
reload it so launchd picks up the change:

```bash
launchctl bootout gui/$(id -u)/com.ryan.autonomous-trader.run-cycle
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
```

## Turning it off

```bash
launchctl bootout gui/$(id -u)/com.ryan.autonomous-trader.run-cycle
rm ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
```

## What happens on a trade that needs approval

Nothing auto-fires and nothing times out -- a trade that lands in
`approval_queue` from an unattended scheduled run just waits there (the
same "safe failure mode" `scripts/review_approvals.py`'s own docstring
already describes) until you run `python scripts/review_approvals.py`
yourself, whenever you next check in. The live dashboard artifact is the
easiest way to see if anything's waiting without needing a terminal open.
