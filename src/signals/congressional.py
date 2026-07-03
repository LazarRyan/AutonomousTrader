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

CALIBRATION STATUS:
  - House (parse_house_ptr_text): VALIDATED against a real filing (Rep.
    Pelosi's PTR #20033725, fetched live). It correctly parsed 17 of 18
    real transaction line items; the 18th was a row split across a PDF
    page boundary by pdfplumber's text extraction (dates/ticker/amount
    landed in a scrambled order relative to the asset name) and was
    correctly flagged rather than mis-parsed or silently dropped -- see
    "KNOWN LIMITATION" below. The parser works on the real, messy text: it
    strips known House PTR boilerplate/header/footer lines and the per-
    transaction "F S: <status>" / "D: <description>" annotation lines, then
    reconstructs each transaction from the remaining text (which still has
    multi-line-wrapped table cells -- asset names and amount ranges commonly
    wrap across the original PDF's line breaks).
  - Senate (parse_senate_ptr_text): VALIDATED against a real filing (Sen.
    Katie Britt's PTR filed 01/26/2026, fetched live -- 22 real spousal
    stock transactions across AAPL, AMZN, GOOG, MSFT, NVDA, UNH, UPS, V,
    WMT, XOM, JPM, EOG). It correctly parsed 21 of 22 real transaction line
    items; the 22nd (an AMZN purchase) was split across a PDF page/line
    boundary by pdfplumber's text extraction -- the ticker landed after the
    comment placeholder on a wrapped line instead of next to the asset name
    -- and was correctly flagged rather than mis-parsed or silently
    dropped. This confirmed the originally-assumed layout (bare "date owner
    ticker asset-name type amount" all on one line) was WRONG for the real
    site: actual rows are prefixed by a row number, and asset names/amounts/
    transaction-type annotations wrap across multiple lines exactly like
    the House filings do. The parser was reworked to use the same clean/
    blob/reconstruct strategy as parse_house_ptr_text() as a direct result
    of this calibration.

KNOWN LIMITATION (both chambers): a transaction row that gets split across
a PDF page or line boundary can have its fields extracted in a scrambled
order (e.g. a ticker and closing amount end up after a comment placeholder
or unrelated header text that repeats at the top of the next page). When
this happens, the transaction fails to match the expected pattern and is
correctly flagged rather than guessed at -- this is the intended "skip and
flag" behavior working as designed, not a bug to silently paper over. It
does mean a small number of real transactions (page-break casualties) will
require manual review via the flagged list rather than being auto-captured.
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
    """Parse an amount string into (low, high). Handles the three shapes
    actually seen in real House PTR filings: a range ("$1,001 - $15,000"),
    an open-ended top bracket ("Over $50,000,000"), and a bare exact dollar
    figure ("$15.00" -- seen on e.g. spinoff/exchange transactions that
    aren't reported as a bracket). Returns None (never raises) if it
    doesn't match a known shape -- caller decides to flag.
    """
    raw = raw.strip()

    over_match = re.match(r"^Over \$([\d,]+)$", raw)
    if over_match:
        return float(over_match.group(1).replace(",", "")), None

    range_match = re.match(r"^\$([\d,]+)\s*-\s*\$([\d,]+)$", raw)
    if range_match:
        low = float(range_match.group(1).replace(",", ""))
        high = float(range_match.group(2).replace(",", ""))
        return low, high

    exact_match = re.match(r"^\$([\d,]+(?:\.\d{2})?)$", raw)
    if exact_match:
        value = float(exact_match.group(1).replace(",", ""))
        return value, value

    return None


# House PTR transaction, reconstructed from the real (messy) extracted PDF
# text -- see CALIBRATION STATUS in the module docstring. Asset names and
# amount ranges routinely wrap across the original PDF's line breaks, so
# this is NOT matched line-by-line: _clean_house_text() first strips known
# boilerplate/header/footer lines and the per-transaction "F S: <status>" /
# "D: <description>" annotation lines, then joins everything else with
# single spaces into one blob, and this pattern is matched against that
# blob with re.finditer (not anchored to line start/end).
#
# asset_name is restricted to [^$] (never crosses a dollar sign) and
# explicitly forbidden from swallowing a standalone owner-code token --
# without both restrictions, a malformed/scrambled transaction earlier in
# the blob can get silently absorbed into a LATER transaction's asset_name
# instead of being left unmatched (and therefore flagged) -- verified
# against the real Pelosi filing during calibration.
_HOUSE_NOISE_LINE_PATTERNS = [
    r"^P T R$",
    r"^Clerk of the House of Representatives",
    r"^F I$",
    r"^Name:\s",
    r"^Status:\s",
    r"^State/District:\s",
    r"^T$",
    r"^ID Owner Asset Transaction$",
    r"^Type$",
    r"^Date Notification$",
    r"^Date Amount Cap\.?$",
    r"^Gains >$",
    r"^\$200\?$",
    r"^Filing ID #",
    r"^\* For the complete list of asset type abbreviations",
    r"^I P O$",
    r"^Yes No$",
    r"^C\s+S$",
    r"^I CERTIFY that the statements",
    r"^my knowledge and belief",
    r"^Digitally Signed:",
]
_HOUSE_TXN_NOTE_LINE_PATTERNS = [
    r"^F\s+S:\s",  # cap-gains-answer + filing status, e.g. "F S: New"
    r"^S:\s",  # standalone filing status line
    r"^D:\s",  # description/detail line (share counts etc. -- not modeled yet)
]
_HOUSE_SKIP_LINE_RE = [
    re.compile(p) for p in _HOUSE_NOISE_LINE_PATTERNS + _HOUSE_TXN_NOTE_LINE_PATTERNS
]

_HOUSE_TRANSACTION_RE = re.compile(
    r"(?:(?P<owner_code>SP|DC|JT)\s+)?"
    r"(?P<asset_name>(?:(?!\b(?:SP|DC|JT)\b\s)[^$]){1,200}?)\s*"
    r"\((?P<ticker>[A-Z]{1,6}(?:\.[A-Z]{1,2})?)\)\s*\[(?P<asset_type>[A-Z]{1,3})\]\s+"
    r"(?P<txn_type>S \(partial\)|S \(full\)|P|S|E)\s+"
    r"(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+(?:\.\d{2})?\s*-\s*\$[\d,]+|Over \$[\d,]+|\$[\d,]+(?:\.\d{2})?)"
)

_HOUSE_TXN_TYPE_MAP = {
    "P": "purchase",
    "S": "sale_full",
    "S (full)": "sale_full",
    "S (partial)": "sale_partial",
    "E": "exchange",
}

# Matches any (TICKER) [TYPE] occurrence, independent of whether a full
# transaction around it matched -- used to find transactions that LOOKED
# like they were starting but didn't fully match, so they can be flagged
# instead of silently vanishing.
_HOUSE_ANCHOR_RE = re.compile(r"\([A-Z]{1,6}(?:\.[A-Z]{1,2})?\)\s*\[[A-Z]{1,3}\]")


def _clean_house_text(text: str) -> str:
    clean_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.match(line) for pattern in _HOUSE_SKIP_LINE_RE):
            continue
        clean_lines.append(line)
    return " ".join(clean_lines)


def parse_house_ptr_text(text: str, source_doc_id: str, filer_name: str) -> ParseResult:
    """Parse the extracted text of one House PTR PDF into transactions.
    See CALIBRATION STATUS in the module docstring -- validated against a
    real filing. Boilerplate/header/footer/annotation lines are stripped
    first (see _clean_house_text), then transactions are reconstructed from
    the remaining text, which still has multi-line-wrapped cells.
    """
    transactions: list[CongressionalTransaction] = []
    flagged: list[FlaggedLine] = []

    cleaned = _clean_house_text(text)
    matches = list(_HOUSE_TRANSACTION_RE.finditer(cleaned))
    covered_spans = [(m.start(), m.end()) for m in matches]

    for match in matches:
        amount = _parse_amount_range(match.group("amount"))
        if amount is None:
            flagged.append(
                FlaggedLine(
                    chamber="house",
                    source_doc_id=source_doc_id,
                    raw_line=match.group(0),
                    reason=f"unrecognized amount format: {match.group('amount')!r}",
                )
            )
            continue

        txn_type = _HOUSE_TXN_TYPE_MAP.get(match.group("txn_type"))
        if txn_type is None:
            flagged.append(
                FlaggedLine(
                    chamber="house",
                    source_doc_id=source_doc_id,
                    raw_line=match.group(0),
                    reason=f"unrecognized transaction type: {match.group('txn_type')!r}",
                )
            )
            continue

        owner_code = _OWNER_CODE_MAP.get(match.group("owner_code") or "", "SELF")
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
                raw_line=match.group(0),
            )
        )

    # Anything that looked like it was starting a transaction (a ticker +
    # asset-type anchor) but isn't covered by a successful match above --
    # most commonly a row mangled by a PDF page break -- gets flagged with
    # surrounding context, never silently dropped.
    for anchor in _HOUSE_ANCHOR_RE.finditer(cleaned):
        already_covered = any(start <= anchor.start() < end for start, end in covered_spans)
        if already_covered:
            continue
        context_start = max(0, anchor.start() - 80)
        context_end = min(len(cleaned), anchor.end() + 100)
        flagged.append(
            FlaggedLine(
                chamber="house",
                source_doc_id=source_doc_id,
                raw_line=cleaned[context_start:context_end].strip(),
                reason="transaction anchor found but full row did not match the expected format (commonly a PDF page-break split)",
            )
        )

    return ParseResult(transactions=transactions, flagged=flagged)


# Senate PTR, reconstructed from the real (messy) extracted PDF text -- see
# CALIBRATION STATUS in the module docstring. Like the House filing, this is
# NOT matched line-by-line: pdfplumber wraps the asset name, dollar amounts,
# and even the transaction-type ("Sale" / "(Partial)") across separate
# lines, and each row is prefixed by its own row number (descending from the
# total transaction count) rather than starting directly with the date.
# _clean_senate_text() strips known boilerplate/header/footer lines first,
# then joins everything else with single spaces into one blob, and this
# pattern is matched against that blob with re.finditer (not anchored to
# line start/end) -- same structure as _HOUSE_TRANSACTION_RE.
_SENATE_NOISE_LINE_PATTERNS = [
    r"^United States Senate$",
    r"^Financial Disclosures$",
    r"^Periodic Transaction Report for ",
    r"^(Mr\.|Mrs\.|Ms\.|Dr\.|The Honorable)\s",
    r"^\s*Filed \d{2}/\d{2}/\d{4} @",
    r"^The following statements were checked before filing:$",
    r"^I certify that the statements I have made on this form are true, complete and correct to the best of$",
    r"^my knowledge and belief\.$",
    r"^I understand that reports cannot be edited once filed\. To make corrections, I will submit an$",
    r"^electronic amendment to this report\.$",
    r"^\s*Transactions \(\d+ transactions? total\)",
    r"^#$",
    r"^Transaction Date Owner Ticker Asset Name$",
    r"^Asset$",
    r"^Type Type Amount Comment$",
    r"^eFD: Print Periodic Transaction Report",
    r"Page \d+ of \d+$",
]
_SENATE_SKIP_LINE_RE = [re.compile(p) for p in _SENATE_NOISE_LINE_PATTERNS]

# asset_name is restricted to [^$] (never crosses a dollar sign) -- without
# this, a malformed row earlier in the blob (e.g. one with an unrecognized
# asset type) can get silently absorbed straight through into a LATER
# transaction's fields, stealing its ticker/date/amount instead of failing
# to match and being flagged. Same fix, same reason, as _HOUSE_TRANSACTION_RE
# -- verified with a regression test built from a real absorption bug found
# during calibration against this parser's first real-filing test run.
_SENATE_TRANSACTION_RE = re.compile(
    r"(?P<row_num>\d{1,3})\s+"
    r"(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<owner>Self|Spouse|Joint|Dependent Child)\s+"
    r"(?P<ticker>[A-Z]{1,6}(?:\.[A-Z]{1,2})?)\s+"
    r"(?P<asset_name>[^$]{1,120}?)\s+"
    r"(?P<asset_type>Stock|Bond|Fund|Option|Other)\s+"
    r"(?P<txn_type>Purchase|Sale \(Full\)|Sale \(Partial\)|Exchange)\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+)"
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

# Matches a "<row#> <date> <owner>" occurrence, independent of whether a
# full transaction around it matched -- used to find rows that LOOKED like
# they were starting a transaction but didn't fully match (most commonly a
# row mangled by a PDF page break, e.g. the ticker landing after the
# comment placeholder on a wrapped line), so they get flagged instead of
# silently vanishing. Same purpose as _HOUSE_ANCHOR_RE.
_SENATE_ROW_ANCHOR_RE = re.compile(r"\d{1,3}\s+\d{2}/\d{2}/\d{4}\s+(?:Self|Spouse|Joint|Dependent Child)\b")


def _clean_senate_text(text: str) -> str:
    clean_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in _SENATE_SKIP_LINE_RE):
            continue
        clean_lines.append(line)
    return " ".join(clean_lines)


def parse_senate_ptr_text(text: str, source_doc_id: str, filer_name: str) -> ParseResult:
    """Parse the extracted text of one Senate PTR PDF into transactions.
    See CALIBRATION STATUS in the module docstring -- validated against a
    real filing. Same skip-and-flag discipline, and the same clean/blob/
    reconstruct strategy, as parse_house_ptr_text()."""
    transactions: list[CongressionalTransaction] = []
    flagged: list[FlaggedLine] = []

    cleaned = _clean_senate_text(text)
    matches = list(_SENATE_TRANSACTION_RE.finditer(cleaned))
    covered_spans = [(m.start(), m.end()) for m in matches]

    for match in matches:
        amount = _parse_amount_range(match.group("amount"))
        if amount is None:
            flagged.append(
                FlaggedLine(
                    chamber="senate",
                    source_doc_id=source_doc_id,
                    raw_line=match.group(0),
                    reason=f"unrecognized amount format: {match.group('amount')!r}",
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
                raw_line=match.group(0),
            )
        )

    # Anything that looked like it was starting a transaction (a row number
    # + date + owner anchor) but isn't covered by a successful match above
    # gets flagged with surrounding context, never silently dropped.
    for anchor in _SENATE_ROW_ANCHOR_RE.finditer(cleaned):
        already_covered = any(start <= anchor.start() < end for start, end in covered_spans)
        if already_covered:
            continue
        context_start = max(0, anchor.start() - 40)
        context_end = min(len(cleaned), anchor.end() + 100)
        flagged.append(
            FlaggedLine(
                chamber="senate",
                source_doc_id=source_doc_id,
                raw_line=cleaned[context_start:context_end].strip(),
                reason="transaction anchor found but full row did not match the expected format (commonly a PDF page-break split)",
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
