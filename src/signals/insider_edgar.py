"""
Insider trade (Form 4) signal via SEC EDGAR.

SEC EDGAR is free, official, and authoritative -- no auth required, no
third-party reseller. Two layers, same split as signals/momentum.py:

  1. PURE, unit-tested logic: parse_form4_xml() turns one Form 4 XML
     document into a list of InsiderTransaction records; compute_insider_signal()
     turns a list of those into a single deterministic score. Both are
     covered by tests/test_insider_edgar.py using fixture XML -- no network.
  2. Thin network wrappers (fetch_ticker_cik_map, fetch_recent_form4_accessions,
     fetch_form4_document, fetch_insider_signal): talk to data.sec.gov and
     www.sec.gov. NOT unit-tested here -- they need live network access and a
     compliant User-Agent header. Exercise these manually once wired into a
     real run.

SEC access requirements (see https://www.sec.gov/os/webmaster-faq#developers):
  - Every request must send a descriptive User-Agent header identifying the
    requester (e.g. "AutonomousTrader ryan@example.com"). Requests without one,
    or with a generic/browser-spoofed one, get blocked.
  - Fair-access rate limit: no more than ~10 requests/second.
data.sec.gov requires no API key.

Scope note: v1 only reads non-derivative transactions (actual common stock
buys/sells), not derivative transactions (options, RSU vesting mechanics,
etc.) -- those are a noisier signal and can be added later. v1 also assumes
one reportingOwner per Form 4 document, which covers the large majority of
real-world filings; a document with multiple reporting owners will have its
relationship flags (officer/director/10%-owner) attributed to the first
listed owner. Both simplifications are intentional for a first pass, not
oversights -- documented here for whoever backtests this next.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

SEC_FAIR_ACCESS_MAX_REQUESTS_PER_SECOND = 10


@dataclass(frozen=True)
class InsiderTransaction:
    symbol: str
    filer_cik: str
    filer_name: str
    is_officer: bool
    is_director: bool
    is_ten_percent_owner: bool
    transaction_date: str  # ISO "YYYY-MM-DD"
    transaction_code: str  # e.g. "P" (open-market purchase), "S" (open-market sale), "A", "F", "M", "G"
    acquired_disposed: str  # "A" = acquired (increases holdings), "D" = disposed (decreases holdings)
    shares: float
    price_per_share: float | None
    shares_owned_after: float | None


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return el.text.strip() if el.text else None


def _value(parent: ET.Element | None, tag: str) -> str | None:
    if parent is None:
        return None
    child = parent.find(tag)
    if child is None:
        return None
    value_el = child.find("value")
    return _text(value_el) if value_el is not None else _text(child)


def parse_form4_xml(xml_content: str, symbol: str) -> list[InsiderTransaction]:
    """Parse one Form 4 ownershipDocument XML string into a list of
    non-derivative transactions. Returns an empty list (not an error) for a
    well-formed document that simply has no non-derivative transactions --
    that's a normal, common shape (e.g. a Form 4 reporting only derivative
    activity). Raises ValueError only for malformed/unparseable XML, per the
    project-wide "skip and flag, never guess" rule for filing data.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse Form 4 XML for {symbol}: {exc}") from exc

    owner_el = root.find("reportingOwner")
    if owner_el is None:
        raise ValueError(f"Form 4 XML for {symbol} has no reportingOwner element")

    owner_id = owner_el.find("reportingOwnerId")
    filer_cik = _text(owner_id.find("rptOwnerCik")) if owner_id is not None else None
    filer_name = _text(owner_id.find("rptOwnerName")) if owner_id is not None else None
    if not filer_cik or not filer_name:
        raise ValueError(f"Form 4 XML for {symbol} is missing reporting owner identity")

    relationship = owner_el.find("reportingOwnerRelationship")

    def _flag(tag: str) -> bool:
        if relationship is None:
            return False
        raw = _text(relationship.find(tag))
        return raw == "1" or (raw or "").lower() == "true"

    is_officer = _flag("isOfficer")
    is_director = _flag("isDirector")
    is_ten_percent_owner = _flag("isTenPercentOwner")

    transactions: list[InsiderTransaction] = []
    non_derivative_table = root.find("nonDerivativeTable")
    if non_derivative_table is None:
        return transactions

    for txn in non_derivative_table.findall("nonDerivativeTransaction"):
        transaction_date = _value(txn, "transactionDate")
        coding = txn.find("transactionCoding")
        transaction_code = _value(coding, "transactionCode") if coding is not None else None

        amounts = txn.find("transactionAmounts")
        shares_raw = _value(amounts, "transactionShares")
        price_raw = _value(amounts, "transactionPricePerShare")
        acquired_disposed = _value(amounts, "transactionAcquiredDisposedCode")

        post_amounts = txn.find("postTransactionAmounts")
        shares_owned_after_raw = _value(post_amounts, "sharesOwnedFollowingTransaction")

        if not transaction_date or not transaction_code or not shares_raw or not acquired_disposed:
            # Incomplete transaction record -- skip and don't guess, rather
            # than fabricate a shares/price value.
            continue

        try:
            shares = float(shares_raw)
        except ValueError:
            continue

        price_per_share = None
        if price_raw is not None:
            try:
                price_per_share = float(price_raw)
            except ValueError:
                price_per_share = None

        shares_owned_after = None
        if shares_owned_after_raw is not None:
            try:
                shares_owned_after = float(shares_owned_after_raw)
            except ValueError:
                shares_owned_after = None

        transactions.append(
            InsiderTransaction(
                symbol=symbol,
                filer_cik=filer_cik,
                filer_name=filer_name,
                is_officer=is_officer,
                is_director=is_director,
                is_ten_percent_owner=is_ten_percent_owner,
                transaction_date=transaction_date,
                transaction_code=transaction_code,
                acquired_disposed=acquired_disposed,
                shares=shares,
                price_per_share=price_per_share,
                shares_owned_after=shares_owned_after,
            )
        )

    return transactions


@dataclass(frozen=True)
class InsiderSignalConfig:
    # Only these transaction codes count toward the signal: P = open-market
    # purchase, S = open-market sale. Excludes grants/awards (A), tax
    # withholding (F), option exercises (M), gifts (G), etc. -- those are not
    # a discretionary "I bought/sold because I think the stock will move"
    # signal the way open-market P/S trades are.
    include_codes: tuple[str, ...] = ("P", "S")

    officer_or_director_weight: float = 1.5
    ten_percent_owner_weight: float = 1.0
    other_weight: float = 1.0

    # Net weighted dollar value (buys minus sells) that maps to a full
    # +/-100 score.
    score_cap_dollars: float = 5_000_000.0

    def __post_init__(self) -> None:
        if self.score_cap_dollars <= 0:
            raise ValueError("score_cap_dollars must be > 0")
        if not self.include_codes:
            raise ValueError("include_codes must be non-empty")


@dataclass(frozen=True)
class InsiderSignalResult:
    symbol: str
    score: float  # -100..100, positive = net insider buying
    net_weighted_dollar_value: float
    num_buy_transactions: int
    num_sell_transactions: int
    num_transactions_considered: int
    reasoning: str


def compute_insider_signal(
    transactions: list[InsiderTransaction], config: InsiderSignalConfig | None = None
) -> InsiderSignalResult:
    """Deterministic composite score from a list of parsed Form 4
    transactions (typically aggregated across several recent filings for one
    symbol). Empty input, or input with no P/S transactions, is a normal
    "no signal" state -- returns score 0.0, not an error.
    """
    config = config or InsiderSignalConfig()

    symbol = transactions[0].symbol if transactions else "UNKNOWN"

    relevant = [t for t in transactions if t.transaction_code in config.include_codes]

    if not relevant:
        return InsiderSignalResult(
            symbol=symbol,
            score=0.0,
            net_weighted_dollar_value=0.0,
            num_buy_transactions=0,
            num_sell_transactions=0,
            num_transactions_considered=0,
            reasoning="no open-market insider purchase/sale transactions found -- neutral",
        )

    for t in relevant:
        if t.shares < 0:
            raise ValueError(f"Negative shares in transaction for {t.symbol}: {t.shares}")
        if t.price_per_share is not None and t.price_per_share < 0:
            raise ValueError(f"Negative price in transaction for {t.symbol}: {t.price_per_share}")
        if t.acquired_disposed not in ("A", "D"):
            raise ValueError(
                f"Unexpected acquired/disposed code for {t.symbol}: {t.acquired_disposed!r}"
            )

    net_weighted_dollar_value = 0.0
    num_buy = 0
    num_sell = 0

    for t in relevant:
        if t.price_per_share is None:
            continue  # can't compute dollar value without a price -- skip, don't guess

        if t.is_officer or t.is_director:
            weight = config.officer_or_director_weight
        elif t.is_ten_percent_owner:
            weight = config.ten_percent_owner_weight
        else:
            weight = config.other_weight

        dollar_value = t.shares * t.price_per_share
        sign = 1.0 if t.acquired_disposed == "A" else -1.0
        net_weighted_dollar_value += sign * dollar_value * weight

        if sign > 0:
            num_buy += 1
        else:
            num_sell += 1

    clipped = max(-config.score_cap_dollars, min(config.score_cap_dollars, net_weighted_dollar_value))
    score = clipped / config.score_cap_dollars * 100.0

    direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    reasoning = (
        f"{direction} insider signal {score:.1f}: {num_buy} buy / {num_sell} sell open-market "
        f"transaction(s), net weighted value ${net_weighted_dollar_value:,.0f}"
    )

    return InsiderSignalResult(
        symbol=symbol,
        score=score,
        net_weighted_dollar_value=net_weighted_dollar_value,
        num_buy_transactions=num_buy,
        num_sell_transactions=num_sell,
        num_transactions_considered=len(relevant),
        reasoning=reasoning,
    )


# ============================================================
# Network wrappers -- not unit-tested here (require live network + a
# compliant User-Agent). See module docstring.
# ============================================================


def fetch_ticker_cik_map(user_agent: str) -> dict[str, str]:
    """Fetch SEC's ticker -> CIK mapping (company_tickers.json), returning
    {TICKER: zero-padded-10-digit-CIK}. Cache this in the caller -- it
    changes rarely and there's no reason to refetch it every cycle.
    """
    import json
    import urllib.request

    request = urllib.request.Request(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": user_agent},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    return {entry["ticker"].upper(): f"{entry['cik_str']:010d}" for entry in data.values()}


def fetch_recent_form4_accessions(
    cik: str, user_agent: str, lookback_days: int = 90
) -> list[dict]:
    """Fetch recent Form 4 filing metadata for one CIK from the submissions
    API, filtered to form type "4" and the lookback window. Returns a list
    of dicts with accessionNumber, filingDate, and primaryDocument -- enough
    to fetch the actual XML via fetch_form4_document().
    """
    import json
    import urllib.request
    from datetime import datetime, timedelta, timezone

    request = urllib.request.Request(
        f"https://data.sec.gov/submissions/CIK{cik}.json",
        headers={"User-Agent": user_agent},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_documents = recent.get("primaryDocument", [])

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)

    results = []
    for form, accession, filing_date, primary_doc in zip(
        forms, accession_numbers, filing_dates, primary_documents
    ):
        if form != "4":
            continue
        if date.fromisoformat(filing_date) < cutoff:
            continue
        results.append(
            {
                "accessionNumber": accession,
                "filingDate": filing_date,
                "primaryDocument": primary_doc,
            }
        )
    return results


def fetch_form4_document(cik: str, accession_number: str, primary_document: str, user_agent: str) -> str:
    """Fetch the raw Form 4 XML for one filing."""
    import urllib.request

    accession_no_dashes = accession_number.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no_dashes}/{primary_document}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8")


def fetch_insider_signal(
    symbol: str,
    cik: str,
    user_agent: str,
    lookback_days: int = 90,
    config: InsiderSignalConfig | None = None,
) -> InsiderSignalResult:
    """End-to-end orchestration: recent Form 4 filings -> parsed transactions
    -> composite signal. Thin glue around the tested pieces above -- not
    itself unit-tested (needs live network).
    """
    accessions = fetch_recent_form4_accessions(cik, user_agent, lookback_days=lookback_days)

    all_transactions: list[InsiderTransaction] = []
    for filing in accessions:
        xml_content = fetch_form4_document(
            cik, filing["accessionNumber"], filing["primaryDocument"], user_agent
        )
        all_transactions.extend(parse_form4_xml(xml_content, symbol))

    return compute_insider_signal(all_transactions, config=config)
