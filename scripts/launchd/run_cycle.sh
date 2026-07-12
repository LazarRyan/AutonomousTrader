#!/bin/bash
# Wrapper invoked by launchd at 9:20am / 1:30pm / 3:50pm ET on weekdays
# (see com.ryan.autonomous-trader.run-cycle.plist in this directory).
#
# launchd does NOT inherit your interactive shell's PATH or conda setup, so
# this hardcodes the project directory and the exact Python interpreter to
# use, rather than relying on `python` resolving correctly. `cd` into the
# project root before running is required for two reasons: python-dotenv's
# load_dotenv() (called from src/config.py) searches the CURRENT WORKING
# DIRECTORY upward for .env, and it won't find it if launchd's default cwd
# (your home directory) is used instead.
#
# main.py's own market-day check (added specifically for this schedule --
# see run_cycle()'s docstring) handles skipping weekends/holidays/early
# closes cleanly with an audit_log row, so this script and its plist don't
# need to duplicate that logic -- it fires every day, and lets the Python
# side decide whether there's actually anything to do.
set -euo pipefail

PROJECT_DIR="/Users/ryanlazar/dev/AutonomousTrader"

PYTHON_BIN="/opt/anaconda3/bin/python"

cd "$PROJECT_DIR"
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') : starting scheduled run_cycle ==="
"$PYTHON_BIN" -m src.main
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') : run_cycle finished (exit code $?) ==="
