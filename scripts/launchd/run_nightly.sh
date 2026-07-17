#!/bin/bash
# Wrapper invoked by launchd at 5:30pm ET on weekdays (see
# com.ryan.autonomous-trader.nightly.plist in this directory).
#
# Same PATH/cwd caveats as run_cycle.sh: launchd inherits neither the
# interactive shell's PATH nor its cwd, and load_dotenv() searches the
# CURRENT WORKING DIRECTORY upward for .env -- hence the explicit cd and
# the hardcoded interpreter.
#
# src/nightly.py does its own trading-day check (Alpaca calendar), so this
# fires every weekday and lets the Python side decide whether there's
# anything to reflect on.
set -euo pipefail

PROJECT_DIR="/Users/ryanlazar/dev/AutonomousTrader"

PYTHON_BIN="/opt/anaconda3/bin/python"

cd "$PROJECT_DIR"
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') : starting scheduled nightly job ==="
"$PYTHON_BIN" -m src.nightly
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') : nightly job finished (exit code $?) ==="
