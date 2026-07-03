"""
Trading universe: S&P 500 constituents (build plan section 4a).

Backed by a static CSV (data/sp500_constituents.csv) rather than fetched
live every cycle -- constituent changes happen a handful of times a year,
not intraday, so there's no reason to hit a network dependency on every run
just to answer "what's in the index." The CSV was seeded from a
well-maintained public dataset (github.com/datasets/s-and-p-500-companies)
and includes each company's SEC CIK, which the insider-trading signal
(signals/insider_edgar.py) needs -- bundling it here means that signal
doesn't have to fetch and parse SEC's full ticker->CIK mapping file on
every run either.

Keep the CSV fresh with scripts/refresh_sp500_universe.py (thin, untested
network wrapper, same pattern as the rest of this project) -- run it every
month or so, or whenever you notice a recent constituent change (additions/
removals get announced by S&P Dow Jones Indices, not embedded in any of
this project's live data sources).
"""

from __future__ import annotations

import csv
from pathlib import Path

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "sp500_constituents.csv"


def _read_rows(csv_path: str | Path | None = None) -> list[dict[str, str]]:
    path = Path(csv_path) if csv_path else DEFAULT_CSV_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"S&P 500 constituents file not found at {path} -- run scripts/refresh_sp500_universe.py"
        )
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_sp500_universe(csv_path: str | Path | None = None) -> list[str]:
    """Return the list of ticker symbols in the trading universe."""
    return [row["symbol"] for row in _read_rows(csv_path)]


def load_sp500_cik_map(csv_path: str | Path | None = None) -> dict[str, str]:
    """Return {ticker: zero-padded-10-digit-CIK}, for signals/insider_edgar.py."""
    return {row["symbol"]: row["cik"] for row in _read_rows(csv_path)}
