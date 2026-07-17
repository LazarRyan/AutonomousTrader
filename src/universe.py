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


# ============================================================
# Full-market universe (2026-07-16, "no more S&P-only" upgrade): every
# active, tradable US equity listed on a real exchange, straight from
# Alpaca's asset master. Replaces the S&P 500 CSV as the discovery FILTER
# (which symbols mentioned in news/congressional filings are eligible at
# all); the CSV stays for the insider signal's bundled CIK map and as the
# fallback when this fetch fails.
# ============================================================


def is_clean_common_stock_symbol(symbol: str) -> bool:
    """Plain-letters tickers only. Alpaca's asset master includes units,
    warrants, preferreds and when-issued lines ('ABC.U', 'ABC.WS', 'ABC-A')
    -- none of which the rest of this pipeline (EDGAR CIK lookup, news
    symbol matching, quote fetches) handles meaningfully, and all of which
    LOOK like cheap 'opportunities' to a signal reading raw mention lists.
    Pure, unit-tested."""
    return symbol.isalpha() and 1 <= len(symbol) <= 5


def fetch_tradable_universe(alpaca_trading_client) -> set[str]:
    """All active, tradable, exchange-listed (not OTC) US equity symbols
    from Alpaca, filtered to clean common-stock tickers. Thin network
    wrapper around the tested is_clean_common_stock_symbol filter --
    ~4-5k names in practice. Raises on API failure; the caller (run_cycle)
    decides the fallback (bundled S&P 500 list), because a silently tiny
    universe is worse than a loudly degraded one."""
    from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    assets = alpaca_trading_client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    return {
        asset.symbol
        for asset in assets
        if asset.tradable and asset.exchange != AssetExchange.OTC and is_clean_common_stock_symbol(asset.symbol)
    }
