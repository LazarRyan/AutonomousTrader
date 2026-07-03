"""
Congressional trade disclosure signal -- House Clerk + Senate eFD.

This is the most fragile signal source in the system, by design of the
source data itself, not this code: House and Senate Periodic Transaction
Reports (PTRs) are published as PDFs (not clean JSON like SEC EDGAR), the
House Clerk and Senate eFD sites have no documented public API, and their
HTML/session/PDF layouts can change without notice. The build plan is
explicit that this needs "skip and flag, never guess" and "budget time for
occasional maintenance."

Two layers, same split as momentum.py / insider_edgar.py:

  1. PURE, unit-tested parsing: parse_house_ptr_text() / parse_senate_ptr_text()
     take already-extracted PDF text and return (transactions, flagged) --
     anything that doesn't match a known row format with confidence goes
     into `flagged`, never into `transactions`. Covered by
     tests/test_congressional.py using fixture text built from the known
     public PTR table layouts.
  2. Thin network/binary wrappers (fetch_house_disclosure_index,
     fetch_house_ptr_pdf, fetch_senate_ptr_listing, fetch_senate_ptr_pdf,
     extract_text_from_pdf): talk to disclosures-clerk.house.gov and
     efdsearch.senate.gov, and do PDF -> text extraction. NOT unit-tested
     here -- they need live network access and, for the Senate, a
     multi-step session/cookie flow (accept the site's terms, then query a
     DataTables JSON endpoint).

IMPORTANT CALIBRATION NOTE: the row-parsing regexes below are built from
the well-documented, publicly known PTR table layouts (the same layouts
used by long-running open-source projects like senate-stock-watcher and
house-stock-watcher), but have NOT been validated against a live-extracted
PDF text sample in this environment. Before this feeds a real signal, pull
a handful of recent real PTR PDFs, run extract_text_from_pdf() +
parse_*_ptr_text() against them, and check the flagged/skipped rate. Treat
a high flagged rate as a sign the regex needs recalibrating, not as a
reason to loosen it and start guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class UnparseableFilingError(Exception):
    """Raised only when an entire filing can't be processed at all (e.g. the
    PDF has no extractable text). Individual unparseable transaction ROWS
    within an otherwise-readable filing are not raised as exceptions --
    they're collected in ParseResult.flagged instead, so one bad row doesn't
    throw away every good row in the same filing.
    """


OwnerCode = str  # "SELF", "SPOUSE", "JOINT", "DEPENDENT_CHILD"

_OWNER_CODE_MAP = {
    "SP": "SPOUSE",
    "DC": "DEPENDENT_CHILD",
    "JT": "JOINT",
}


@dataclass(frozen=True)
class CongressionalTransaction:
    chamber: str  # "house" or "senate"
    source_doc_id: str
    filer_name: str
    owner: OwnerCode
    ticker: str
    asset_name: str
    transaction_type: str  # "purchase", "sale_full", "sale_partial", "exchange"
    transaction_date: str  # ISO "YYYY-MM-DD"
    amount_low: float
    amount_high: float | None  # None for open-ended top bracket ("Over $X")
    raw_line: str


@dataclass(frozen=True)
class FlaggedLine:
    chamber: str
    source_doc_id: str
    raw_line: str
    reason: str


@dataclass(frozen=True)
class ParseResult:
    transactions: list[CongressionalTransaction] = field(default_factory=list)
    flagged: list[FlaggedLine] = field(default_factory=list)


def _parse_date_mmddyyyy(raw: str) -> str:
    month, day, year = raw.split("/")
    return f"{year}-{month}-{day}"


def _parse_amount_range(raw: str) -> tuple[float, float | None] | None:
    """Parse an amount-range string into (low, high). Returns None (never
    raises) if it doesn't match a known shape -- caller decides to flag."""
    raw = raw.strip()

    over_match = re.match(r"^Over \$([\d,]+)$", raw)
    if over_match:
        return float(over_match.group(1).replace(",", "")), None

    range_match = re.match(r"^\$([\d,]+)\s*-\s*\$([\d,]+)$", raw)
    if range_match:
        low = float(range_match.group(1).replace(",", ""))
        high = float(range_match.group(2).replace(",", ""))
        return low, high

    return None


# House PTR row, e.g.:
#   "SP Apple Inc. (AAPL) [ST] P 01/15/2026 02/03/2026 $1,001 - $15,000"
#   "Microsoft Corp (MSFT) [ST] S (partial) 03/02/2026 03/20/2026 $15,001 - $50,000"
_HOUSE_ROW_RE = re.compile(
    r"^(?:(?P<owner_code>SP|DC|JT)\s+)?"
    r"(?P<asset_name>.+?)\s*\((?P<ticker>[A-Z]{1,6}(?:\.[A-Z]{1,2})?)\)\s*"
    r"\[(?P<asset_type>[A-Z]{1,3})\]\s+"
    r"(?P<txn_type>P|S \(partial\)|S \(full\)|S|E)\s+"
    r"(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>Over \$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+)\s*$"
)

_HOUSE_TXN_TYPE_MAP = {
    "P": "purchase",
    "S": "sale_full",
    "S (full)": "sale_full",
    "S (partial)": "sale_partial",
    "E": "exchange",
}


def parse_house_ptr_text(text: str, source_doc_id: str, filer_name: str) -> ParseResult:
    """Parse the extracted text of one House PTR PDF into transactions.
    Only the transaction table rows matter here -- everything else in the
    PDF (header, certification boilerplate, page numbers) is expected not
    to match the row regex and is silently ignored, NOT flagged, since it
    was never supposed to look like a transaction row in the first place.
    Lines that look like they're trying to be a transaction row (contain a
    ticker in parens and a dollar amount) but don't fully match get flagged.
    """
    transactions: list[CongressionalTransaction] = []
    flagged: list[FlaggedLine] = []

    looks_like_row = re.compile(r"\([A-Z]{1,6}(?:\.[A-Z]{1,2})?\).*\$[\d,]")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _HOUSE_ROW_RE.match(line)
        if not match:
            if looks_like_row.search(line):
                flagged.append(
                    FlaggedLine(
                        chamber="house",
                        source_doc_id=source_doc_id,
                        raw_line=line,
                        reason="line resembles a transaction row but did not match the expected House PTR format",
                    )
                )
            continue

        amount = _parse_amount_range(match.group("amount"))
        if amount is None:
            flagged.append(
                FlaggedLine(
                    chamber="house",
                    source_doc_id=source_doc_id,
                    raw_line=line,
                    reason=f"unrecognized amount range format: {match.group('amount')!r}",
                )
            )
            continue

        owner_code = _OWNER_CODE_MAP.get(match.group("owner_code") or "", "SELF")
        txn_type = _HOUSE_TXN_TYPE_MAP.get(match.group("txn_type"))
        if txn_type is None:
            flagged.append(
                FlaggedLine(
                    chamber="house",
                    source_doc_id=source_doc_id,
                    raw_line=line,
                    reason=f"unrecognized transaction type: {match.group('txn_type')!r}",
                )
            )
            continue

        amount_low, amount_high = amount
        transactions.append(
            CongressionalTransaction(
                chamber="house",
                source_doc_id=source_doc_id,
                filer_name=filer_name,
                owner=owner_code,
                ticker=match.group("ticker"),
                asset_name=match.group("asset_name").strip(),
                transaction_type=txn_type,
                transaction_date=_parse_date_mmddyyyy(match.group("txn_date")),
                amount_low=amount_low,
                amount_high=amount_high,
                raw_line=line,
            )
        )

    return ParseResult(transactions=transactions, flagged=flagged)


# Senate PTR row, e.g.:
#   "01/15/2026 Self AAPL Apple Inc. Stock Purchase $1,001 - $15,000"
#   "03/02/2026 Spouse MSFT Microsoft Corp Stock Sale (Partial) $15,001 - $50,000"
_SENATE_ROW_RE = re.compile(
    r"^(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<owner>Self|Spouse|Joint|Dependent Child)\s+"
    r"(?P<ticker>[A-Z]{1,6}(?:\.[A-Z]{1,2})?)\s+"
    r"(?P<asset_name>.+?)\s+"
    r"(?:Stock|Bond|Fund|Option|Other)\s+"
    r"(?P<txn_type>Purchase|Sale \(Full\)|Sale \(Partial\)|Exchange)\s+"
    r"(?P<amount>Over \$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+)\s*$"
)

_SENATE_OWNER_MAP = {
    "Self": "SELF",
    "Spouse": "SPOUSE",
    "Joint": "JOINT",
    "Dependent Child": "DEPENDENT_CHILD",
}

_SENATE_TXN_TYPE_MAP = {
    "Purchase": "purchase",
    "Sale (Full)": "sale_full",
    "Sale (Partial)": "sale_partial",
    "Exchange": "exchange",
}


def parse_senate_ptr_text(text: str, source_doc_id: str, filer_name: str) -> ParseResult:
    """Parse the extracted text of one Senate PTR PDF into transactions.
    Same skip-and-flag discipline as parse_house_ptr_text()."""
    transactions: list[CongressionalTransaction] = []
    flagged: list[FlaggedLine] = []

    looks_like_row = re.compile(r"^\d{2}/\d{2}/\d{4}\s+\S")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SENATE_ROW_RE.match(line)
        if not match:
            if looks_like_row.match(line):
                flagged.append(
                    FlaggedLine(
                        chamber="senate",
                        source_doc_id=source_doc_id,
                        raw_line=line,
                        reason="line resembles a transaction row but did not match the expected Senate PTR format",
                    )
                )
            continue

        amount = _parse_amount_range(match.group("amount"))
        if amount is None:
            flagged.append(
                FlaggedLine(
                    chamber="senate",
                    source_doc_id=source_doc_id,
                    raw_line=line,
                    reason=f"unrecognized amount range format: {match.group('amount')!r}",
                )
            )
            continue

        amount_low, amount_high = amount
        transactions.append(
            CongressionalTransaction(
                chamber="senate",
                source_doc_id=source_doc_id,
                filer_name=filer_name,
                owner=_SENATE_OWNER_MAP[match.group("owner")],
                ticker=match.group("ticker"),
                asset_name=match.group("asset_name").strip(),
                transaction_type=_SENATE_TXN_TYPE_MAP[match.group("txn_type")],
                transaction_date=_parse_date_mmddyyyy(match.group("txn_date")),
                amount_low=amount_low,
                amount_high=amount_high,
                raw_line=line,
            )
        )

    return ParseResult(transactions=transactions, flagged=flagged)


def parse_ptr_text(text: str, chamber: str, source_doc_id: str, filer_name: str) -> ParseResult:
    if chamber == "house":
        return parse_house_ptr_text(text, source_doc_id, filer_name)
    if chamber == "senate":
        return parse_senate_ptr_text(text, source_doc_id, filer_name)
    raise ValueError(f"Unknown chamber {chamber!r}, expected 'house' or 'senate'")


@dataclass(frozen=True)
class CongressionalSignalConfig:
    amount_midpoint_for_open_ended: float = 1.5  # multiplier applied to `amount_low` when amount_high is None
    score_cap_dollars: float = 1_000_000.0


@dataclass(frozen=True)
class CongressionalSignalResult:
    ticker: str
    score: float  # -100..100, positive = net congressional buying
    net_dollar_value_midpoint: float
    num_buy_transactions: int
    num_sell_transactions: int
    num_flagged: int
    reasoning: str


def compute_congressional_signal(
    transactions: list[CongressionalTransaction],
    flagged: list[FlaggedLine] | None = None,
    config: CongressionalSignalConfig | None = None,
) -> CongressionalSignalResult:
    """Deterministic composite score from parsed PTR transactions for one
    ticker (aggregate across however many filings/lines were found in the
    lookback window). Amount ranges are disclosed as brackets, not exact
    dollar figures, so this necessarily uses the bracket midpoint as an
    estimate -- flagged as an approximation in the reasoning string, not
    hidden.
    """
    config = config or CongressionalSignalConfig()
    flagged = flagged or []

    ticker = transactions[0].ticker if transactions else "UNKNOWN"

    if not transactions:
        return CongressionalSignalResult(
            ticker=ticker,
            score=0.0,
            net_dollar_value_midpoint=0.0,
            num_buy_transactions=0,
            num_sell_transactions=0,
            num_flagged=len(flagged),
            reasoning="no parseable congressional transactions found -- neutral",
        )

    net_value = 0.0
    num_buy = 0
    num_sell = 0

    for t in transactions:
        if t.amount_high is not None:
            midpoint = (t.amount_low + t.amount_high) / 2.0
        else:
            midpoint = t.amount_low * config.amount_midpoint_for_open_ended

        if t.transaction_type == "purchase":
            net_value += midpoint
            num_buy += 1
        elif t.transaction_type in ("sale_full", "sale_partial"):
            net_value -= midpoint
            num_sell += 1
        # "exchange" is neither a clean buy nor sell signal -- excluded from
        # the dollar math but still counted in num_transactions upstream via
        # len(transactions).

    clipped = max(-config.score_cap_dollars, min(config.score_cap_dollars, net_value))
    score = clipped / config.score_cap_dollars * 100.0

    direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    reasoning = (
        f"{direction} congressional signal {score:.1f} for {ticker}: {num_buy} buy / {num_sell} sell "
        f"transaction(s), net estimated (bracket-midpoint) value ${net_value:,.0f}"
        + (f"; {len(flagged)} line(s) skipped as unparseable -- see audit log" if flagged else "")
    )

    return CongressionalSignalResult(
        ticker=ticker,
        score=score,
        net_dollar_value_midpoint=net_value,
        num_buy_transactions=num_buy,
        num_sell_transactions=num_sell,
        num_flagged=len(flagged),
        reasoning=reasoning,
    )


# ============================================================
# Network / binary wrappers -- not unit-tested here (require live network,
# and for the Senate, a multi-step session flow). See module docstring's
# calibration note before relying on these.
# ============================================================


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract raw text from a PTR PDF. Uses pdfplumber. Scanned/image-only
    PDFs (no embedded text layer) will return empty or garbled text --
    callers should treat an empty-text result as "could not extract",
    raise UnparseableFilingError, and skip+flag the whole filing rather than
    silently proceeding with nothing.
    """
    import io

    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    text = "\n".join(text_parts)
    if not text.strip():
        raise UnparseableFilingError("No extractable text in PDF -- likely a scanned/image-only filing")
    return text


def fetch_house_disclosure_index(year: int, user_agent: str) -> list[dict]:
    """Download and parse the House Clerk's annual financial disclosure
    index (a ZIP containing an FD.xml index of every filing that year),
    filtered to periodic transaction reports (FilingType == "P").

    Returns dicts with docId, last, first, filingType, filingDate.
    """
    import io
    import urllib.request
    import zipfile
    from xml.etree import ElementTree as ET

    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        zip_bytes = response.read()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_name = next(name for name in zf.namelist() if name.upper().endswith("FD.XML"))
        xml_bytes = zf.read(xml_name)

    root = ET.fromstring(xml_bytes)
    results = []
    for member in root.findall("Member"):
        filing_type = (member.findtext("FilingType") or "").strip()
        if filing_type != "P":
            continue
        results.append(
            {
                "docId": (member.findtext("DocID") or "").strip(),
                "last": (member.findtext("Last") or "").strip(),
                "first": (member.findtext("First") or "").strip(),
                "filingType": filing_type,
                "filingDate": (member.findtext("FilingDate") or "").strip(),
            }
        )
    return results


def fetch_house_ptr_pdf(doc_id: str, year: int, user_agent: str) -> bytes:
    import urllib.request

    url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_senate_ptr_listing(user_agent: str, start_date: str, end_date: str) -> list[dict]:
    """Query efdsearch.senate.gov for recent PTR filings between two dates
    (MM/DD/YYYY strings). Requires a two-step session flow: accept the
    site's terms to get a session cookie, then POST to the DataTables JSON
    search endpoint.

    NOT validated against the live site in this environment -- the site's
    form field names, CSRF token handling, and response shape are the parts
    most likely to drift over time. Verify this against a live request
    before relying on it, and expect to fix field names here occasionally.
    """
    import urllib.parse
    import urllib.request
    from http.cookiejar import CookieJar

    cookie_jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [("User-Agent", user_agent)]

    home_url = "https://efdsearch.senate.gov/search/home/"
    opener.open(home_url, timeout=30).read()

    csrf_token = None
    for cookie in cookie_jar:
        if cookie.name == "csrftoken":
            csrf_token = cookie.value
    if not csrf_token:
        raise UnparseableFilingError("Could not obtain CSRF token from efdsearch.senate.gov")

    agree_data = urllib.parse.urlencode(
        {"csrfmiddlewaretoken": csrf_token, "prohibition_agreement": "1"}
    ).encode()
    agree_request = urllib.request.Request(home_url, data=agree_data, headers={"User-Agent": user_agent})
    opener.open(agree_request, timeout=30).read()

    search_url = "https://efdsearch.senate.gov/search/report/data/"
    search_data = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": csrf_token,
            "report_type": "11",  # PTR report type code
            "submitted_start_date": start_date,
            "submitted_end_date": end_date,
        }
    ).encode()
    search_request = urllib.request.Request(
        search_url, data=search_data, headers={"User-Agent": user_agent, "Referer": home_url}
    )
    import json

    response_body = opener.open(search_request, timeout=30).read()
    payload = json.loads(response_body)

    return payload.get("data", [])


def fetch_senate_ptr_pdf(relative_url: str, user_agent: str) -> bytes:
    import urllib.request

    url = f"https://efdsearch.senate.gov{relative_url}"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()
