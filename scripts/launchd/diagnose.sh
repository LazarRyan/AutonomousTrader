#!/bin/bash
# Diagnose why the launchd schedule and/or macOS notifications aren't firing.
# Run on your Mac:  bash scripts/launchd/diagnose.sh
# Read-only -- checks state, changes nothing.

LABEL="com.ryan.autonomous-trader.run-cycle"
REPO_PLIST="/Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/${LABEL}.plist"
INSTALLED_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/autonomous-trader"
PYTHON_BIN="/opt/anaconda3/bin/python"
WRAPPER="/Users/ryanlazar/dev/AutonomousTrader/scripts/launchd/run_cycle.sh"

pass() { echo "  [ok]   $1"; }
fail() { echo "  [FAIL] $1"; }

echo "== 1. Is the plist installed in ~/Library/LaunchAgents? =="
if [ -f "$INSTALLED_PLIST" ]; then
  pass "installed plist exists"
  if diff -q "$INSTALLED_PLIST" "$REPO_PLIST" >/dev/null 2>&1; then
    pass "installed plist matches the repo copy"
  else
    fail "installed plist DIFFERS from repo copy -- launchd is using the OLD schedule."
    echo "         Fix: cp \"$REPO_PLIST\" ~/Library/LaunchAgents/"
    echo "              launchctl bootout gui/\$(id -u)/$LABEL"
    echo "              launchctl bootstrap gui/\$(id -u) \"$INSTALLED_PLIST\""
    echo "         Schedule diff (installed vs repo):"
    diff "$INSTALLED_PLIST" "$REPO_PLIST" | sed 's/^/         /'
  fi
else
  fail "NOT installed -- launchd has no idea this job exists. This alone explains zero scheduled runs."
  echo "         Fix: cp \"$REPO_PLIST\" ~/Library/LaunchAgents/  then bootstrap it (see README step 4-5)."
fi

echo
echo "== 2. Is the job loaded into launchd? =="
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  pass "job is loaded"
  echo "  --- launchctl print (state / last exit status / runs):"
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
    | grep -Ei "state|last exit|runs|spawn|error" | sed 's/^/  /'
else
  fail "job is NOT loaded. If the plist exists (check 1), it was never bootstrapped or macOS disabled it."
  echo "         Fix: launchctl bootstrap gui/\$(id -u) \"$INSTALLED_PLIST\""
  echo "         Also check System Settings > General > Login Items & Extensions:"
  echo "         look for a disabled background item (may show as 'bash' or your username)."
fi

echo
echo "== 3. Does the log directory exist? (launchd won't create it; a missing dir makes every spawn fail silently) =="
if [ -d "$LOG_DIR" ]; then
  pass "log dir exists"
  echo "  --- last lines of each log (empty/missing files mean the job never spawned):"
  for f in "$LOG_DIR/run-cycle.log" "$LOG_DIR/run-cycle.err.log"; do
    echo "  $f:"
    [ -f "$f" ] && tail -5 "$f" | sed 's/^/    /' || echo "    (does not exist)"
  done
else
  fail "log dir MISSING. launchd cannot open StandardOutPath, so the job fails to spawn every time."
  echo "         Fix: mkdir -p \"$LOG_DIR\""
fi

echo
echo "== 4. Wrapper script and interpreter =="
[ -x "$WRAPPER" ] && pass "run_cycle.sh is executable" \
  || fail "run_cycle.sh is NOT executable. Fix: chmod +x \"$WRAPPER\""
[ -x "$PYTHON_BIN" ] && pass "$PYTHON_BIN exists" \
  || fail "$PYTHON_BIN does not exist -- fix PYTHON_BIN in run_cycle.sh (use: which python)"

echo
echo "== 5. Notifications =="
APPLET="$HOME/Applications/AutonomousTraderNotifier.app"
if command -v terminal-notifier >/dev/null 2>&1; then
  pass "terminal-notifier installed ($(command -v terminal-notifier)) -- the preferred backend"
  echo "         If notifications don't appear: (1) REBOOT -- confirmed on this machine that"
  echo "         terminal-notifier doesn't register with macOS until after a restart;"
  echo "         (2) System Settings > Notifications > terminal-notifier -> Allow, Alerts/Banners;"
  echo "         (3) check no Focus/Do Not Disturb mode is active."
elif [ -d "$APPLET" ]; then
  pass "notifier applet built ($APPLET) -- fallback backend (terminal-notifier not installed)"
  echo "         If notifications don't appear: System Settings > Notifications >"
  echo "         AutonomousTraderNotifier -> Allow; try a reboot; check Focus is off."
else
  fail "no working backend: terminal-notifier not installed and applet not built."
  echo "         Fix: brew install terminal-notifier   then send a test; if nothing appears, REBOOT"
  echo "         and test again (required once on this machine before it registered)."
fi
echo "  Send a test notification now:"
echo "    $PYTHON_BIN -c \"import sys; sys.path.insert(0,'.'); from src.notify import send_macos_notification as n; n('Autonomous Trader','test')\""

echo
echo "== 6. Fire the job right now (optional -- REAL run against your paper account) =="
echo "    launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "    tail -f \"$LOG_DIR/run-cycle.log\""
