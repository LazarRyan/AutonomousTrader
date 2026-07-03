"""
Refresh data/sp500_constituents.csv from a maintained public source.

Thin network wrapper, not unit-tested (same pattern as the rest of this
project's network glue). Run this every month or so to keep the trading
universe in sync with real S&P 500 constituent changes.

Run with: python scripts/refresh_sp500_universe.py
"""

from __future__ import annotations

import csv
import io
import urllib.request

SOURCE_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
OUTPUT_PATH = "data/sp500_constituents.csv"


def refresh() -> int:
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "autonomous-trader"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw_csv = response.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(raw_csv))
    rows = [{"symbol": row["Symbol"], "security": row["Security"], "cik": row["CIK"]} for row in reader]

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "security", "cik"])
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


if __name__ == "__main__":
    count = refresh()
    print(f"Wrote {count} constituents to {OUTPUT_PATH}")
