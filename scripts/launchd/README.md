# launchd schedule: 10:00am / 12:45pm / 3:30pm ET

Runs `python -m src.main` (the real scheduled entry point -- places real
paper trades, same as everything else in this project) three times a day
instead of continuously. Weekends, holidays, and early closes are handled
by `run_cycle()`'s own market-day check (added specifically for this),
not by launchd -- see `src/main.py`'s docstring.

Re-timed 2026-07-16 (was 9:20am / 1:30pm / 3:50pm). The old 9:20am slot
fired pre-open so orders queued for the opening cross -- which meant risk
scoring ran on thin pre-market IEX quotes while fills happened in the
opening auction, the widest-spread window of the day. The current slots
trade against live, settled quotes instead: 10:00am (post-open, overnight
news and filings digested), 12:45pm (midday news check), 3:30pm (full-day
picture, with room for fills before the closing auction). If you change
these, also update src/discovery.py's DailySchedule.slot_times -- it
drives the news-lookback windows and must match this plist.

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
   chmod +x /Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/run_cycle.sh
   ```

3. **Create the log directory** (launchd won't create it for you):

   ```bash
   mkdir -p ~/Library/Logs/autonomous-trader
   ```

4. **Copy the plist into LaunchAgents:**

   ```bash
   cp /Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/com.ryan.autonomous-trader.run-cycle.plist \
      ~/Library/LaunchAgents/
   ```

5. **Load it:**

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
   ```

   (On older macOS versions where `bootstrap` isn't available, use
   `launchctl load ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist`
   instead.)

## Do NOT keep this project in ~/Documents (or Desktop/Downloads)

Real failure found in practice (2026-07-06, the first attempted scheduled
runs): with the project in `~/Documents`, every launchd firing died with

```
/bin/bash: .../run_cycle.sh: Operation not permitted
```

before the script ran at all -- macOS's TCC privacy protection covers
Documents/Desktop/Downloads, and the grant your Terminal has doesn't
extend to a bash process spawned by launchd in the background. One run got
far enough for python to start and then failed with
`No module named 'src'` -- same cause (python couldn't read the project
contents inside the protected folder), not a real import problem.

Fix used: move the project to an unprotected path (`~/dev/AutonomousTrader`),
update the paths in `run_cycle.sh` and the `.plist`, re-copy the plist, and
reload. The alternative -- granting `/bin/bash` Full Disk Access -- works
but extends that grant to every launchd bash script on the machine, so it
was deliberately not used.

`diagnose.sh` in this directory checks for this and everything else in
this README (installed-plist drift, load state, missing log dir,
interpreter path, terminal-notifier) -- run it first whenever scheduled
runs aren't happening.

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

If you edit `run_cycle.sh`, reload it the same way (it's invoked by path,
so no copy step needed for that one):

```bash
launchctl bootout gui/$(id -u)/com.ryan.autonomous-trader.run-cycle
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
```

If you edit the `.plist` itself (e.g. changing the schedule times), you
MUST re-copy it into `~/Library/LaunchAgents/` first -- `bootstrap` loads
from that copy, not from the repo file directly, so skipping this step
silently reloads the OLD schedule with no error or warning:

```bash
cp /Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/com.ryan.autonomous-trader.run-cycle.plist \
   ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.ryan.autonomous-trader.run-cycle
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist
```

Verify the copy actually took (compares the live copy against the repo's):

```bash
diff ~/Library/LaunchAgents/com.ryan.autonomous-trader.run-cycle.plist \
     /Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/com.ryan.autonomous-trader.run-cycle.plist \
  && echo "in sync" || echo "MISMATCH -- re-copy and reload"
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

You'll also get a native macOS notification the moment it's queued (see
`src/notify.py`), and a second notification at the end of every cycle
summarizing what happened (executed/queued/blocked, or "no trades
proposed"). Both are best-effort -- see the next section if you don't see
them.

## macOS notification setup (one-time)

Install terminal-notifier, send a test, and -- the step that actually
mattered on this machine -- **reboot if nothing appears**:

```bash
brew install terminal-notifier
python -c "from src.notify import send_macos_notification as n; n('Autonomous Trader', 'test notification')"
```

Real sequence observed here (2026-07-06): right after install,
terminal-notifier exited 0 but posted nothing -- no banner, no permission
prompt, not even an entry in System Settings -> Notifications, and
launching its .app bundle directly didn't register it either. After a
machine RESTART it appeared in System Settings -> Notifications, could be
enabled, and worked. So: if it looks broken right after install, reboot
before debugging anything else. Once working, confirm alerts/banners are
enabled under its name in System Settings -> Notifications if you ever
stop seeing them.

`src/notify.py` tries terminal-notifier first, then a locally-compiled
AppleScript applet (`~/Applications/AutonomousTraderNotifier.app`, built
by `scripts/setup_notifier_applet.sh` -- created as a replacement during
the pre-reboot window when terminal-notifier appeared broken, kept as a
fallback for machines without terminal-notifier; note it did NOT visibly
post on this machine), then bare
`osascript -e 'display notification ...'` (confirmed a silent no-op on
current macOS even after a restart -- no app identity behind it). All
three are checked fresh on every call, so installing/removing any of them
needs no code change or reload.
