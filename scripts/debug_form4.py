"""
One-off diagnostic for the Form 4 XML parse failure hit during the first
live dry run ("Could not parse Form 4 XML for AAPL: mismatched tag: line
29, column 16"). Fetches the exact same real document the failing signal
fetched and prints the lines around the parse error so the actual cause
can be diagnosed from real content instead of guessed at.

Run with: python scripts/debug_form4.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv

load_dotenv()

from src.signals.insider_edgar import fetch_form4_document, fetch_recent_form4_accessions, fetch_ticker_cik_map


def main() -> None:
    user_agent = os.getenv("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        raise RuntimeError("SEC_EDGAR_USER_AGENT must be set -- see .env.example")

    symbol = "AAPL"
    cik_map = fetch_ticker_cik_map(user_agent)
    cik = cik_map[symbol]
    print(f"{symbol} CIK: {cik}")

    accessions = fetch_recent_form4_accessions(cik, user_agent, lookback_days=90)
    print(f"Found {len(accessions)} recent Form 4 filings")
    if not accessions:
        print("No recent Form 4 filings to test against -- widen lookback_days and rerun.")
        return

    filing = accessions[0]
    print(f"Fetching: accession={filing['accessionNumber']} primaryDocument={filing['primaryDocument']}")

    xml_content = fetch_form4_document(cik, filing["accessionNumber"], filing["primaryDocument"], user_agent)

    out_path = "form4_debug.xml"
    with open(out_path, "w") as f:
        f.write(xml_content)
    print(f"Saved full raw response to {out_path} ({len(xml_content)} chars)")

    lines = xml_content.splitlines()
    print(f"\nTotal lines: {len(lines)}")
    print("\n--- lines 1-10 (to check it's really XML and not an HTML error page) ---")
    for i, line in enumerate(lines[:10], start=1):
        print(f"{i:4d}: {line}")

    print("\n--- lines 20-35 (around the reported parse error at line 29) ---")
    for i, line in enumerate(lines[19:35], start=20):
        print(f"{i:4d}: {line}")

    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(xml_content)
        print("\nParsed successfully this time (?) -- error may be intermittent or filing-specific.")
    except ET.ParseError as exc:
        print(f"\nConfirmed parse error: {exc}")


if __name__ == "__main__":
    main()
