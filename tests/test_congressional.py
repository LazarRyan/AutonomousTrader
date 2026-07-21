from datetime import date

import pytest

from src.signals.congressional import (
    CongressionalSignalConfig,
    CongressionalTransaction,
    FlaggedLine,
    ParseResult,
    aggregate_transactions_by_ticker,
    compute_congressional_signal,
    filter_filings_by_date,
    parse_house_ptr_text,
    parse_ptr_text,
    parse_result_from_cache_dict,
    parse_result_to_cache_dict,
    parse_senate_ptr_text,
)


def _house_pdf_row(asset_line: str, next_line: str, txn_type: str, txn_date: str, notif_date: str, amount_lines: list[str]) -> str:
    """Builds a realistic multi-line House PTR row the way pdfplumber's
    extract_text() actually renders it -- asset name wraps onto its own
    line(s), then the (TICKER) [TYPE] tag, then type/dates/amount, with
    amount ranges commonly wrapping across a "$X -" / "$Y" line break, and
    an "F S: New" + "D: ..." annotation pair following. Mirrors the real
    Pelosi PTR sample used in TestParseHousePtrTextRealFiling below.
    """
    lines = [asset_line, next_line, f"{txn_type} {txn_date} {notif_date} {amount_lines[0]}"]
    lines.extend(amount_lines[1:])
    lines.append("F S: New")
    lines.append("D: some description text.")
    return "\n".join(lines)


class TestParseHousePtrText:
    def test_parses_a_clean_purchase_row(self):
        text = _house_pdf_row(
            "Apple Inc. - Common Stock", "(AAPL) [ST]", "P", "01/15/2026", "02/03/2026", ["$1,001 -", "$15,000"]
        )
        result = parse_house_ptr_text(text, "20033725", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.chamber == "house"
        assert t.ticker == "AAPL"
        assert t.asset_name == "Apple Inc. - Common Stock"
        assert t.transaction_type == "purchase"
        assert t.transaction_date == "2026-01-15"
        assert t.amount_low == 1001.0
        assert t.amount_high == 15000.0
        assert t.owner == "SELF"
        assert result.flagged == []

    def test_parses_owner_code_prefix(self):
        text = _house_pdf_row(
            "SP Microsoft Corp", "(MSFT) [ST]", "S (partial)", "03/02/2026", "03/20/2026", ["$15,001 -", "$50,000"]
        )
        result = parse_house_ptr_text(text, "20033726", "SMITH JOHN")
        t = result.transactions[0]
        assert t.owner == "SPOUSE"
        assert t.transaction_type == "sale_partial"
        assert t.ticker == "MSFT"

    def test_parses_dotted_ticker_and_over_bracket_amount(self):
        text = _house_pdf_row(
            "Berkshire Hathaway", "(BRK.A) [ST]", "P", "01/01/2026", "01/20/2026", ["Over $50,000,000"]
        )
        result = parse_house_ptr_text(text, "20033727", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.ticker == "BRK.A"
        assert t.amount_low == 50_000_000.0
        assert t.amount_high is None
        assert result.flagged == []

    def test_parses_bare_exact_dollar_amount(self):
        # Seen on real filings for exchange/spinoff transactions, e.g. "$15.00"
        text = _house_pdf_row(
            "Versant Media Group, Inc. - Class A Common Stock", "(VSNT) [ST]", "E", "01/02/2026", "01/02/2026", ["$15.00"]
        )
        result = parse_house_ptr_text(text, "20033728", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.transaction_type == "exchange"
        assert t.amount_low == 15.0
        assert t.amount_high == 15.0

    def test_boilerplate_lines_are_ignored_not_flagged(self):
        text = "\n".join(
            [
                "P T R",
                "Clerk of the House of Representatives • Legislative Resource Center • B81 Cannon Building • Washington, DC 20515",
                "Name: John Smith",
                "Status: Member",
                "State/District: CA11",
                "ID Owner Asset Transaction",
                "Type",
                "Date Notification",
                "Date Amount Cap.",
                "Gains >",
                "$200?",
                "Filing ID #20033729",
                "I CERTIFY that the statements I have made on the attached Periodic Transaction Report are true, complete, and correct to the best of",
                "my knowledge and belief. Further, I CERTIFY that I have disclosed all transactions as required by the STOCK Act.",
                "Digitally Signed: John Smith , 01/23/2026",
            ]
        )
        result = parse_house_ptr_text(text, "20033729", "SMITH JOHN")
        assert result.transactions == []
        assert result.flagged == []  # these lines never looked like transaction rows

    def test_row_that_looks_like_transaction_but_malformed_is_flagged(self):
        text = _house_pdf_row(
            "Apple Inc.", "(AAPL) [ST]", "Q", "01/15/2026", "02/03/2026", ["$1,001 -", "$15,000"]
        )  # "Q" isn't a valid txn type
        result = parse_house_ptr_text(text, "20033730", "SMITH JOHN")
        assert result.transactions == []
        assert len(result.flagged) == 1

    def test_multiple_rows_mixed_clean_and_flagged(self):
        good1 = _house_pdf_row(
            "Apple Inc.", "(AAPL) [ST]", "P", "01/15/2026", "02/03/2026", ["$1,001 -", "$15,000"]
        )
        good2 = _house_pdf_row(
            "Microsoft Corp", "(MSFT) [ST]", "S", "03/02/2026", "03/20/2026", ["$15,001 -", "$50,000"]
        )
        bad = _house_pdf_row(
            "Some Garbage Corp", "(XX) [QQ]", "Z", "01/01/2026", "01/01/2026", ["$5.00"]
        )  # "Z" isn't a valid txn type
        text = "\n".join([good1, bad, good2])
        result = parse_house_ptr_text(text, "20033731", "SMITH JOHN")
        assert len(result.transactions) == 2
        assert len(result.flagged) == 1


class TestParseHousePtrTextRealFiling:
    """Validated against a real, live-fetched House PTR filing (Rep. Nancy
    Pelosi, Filing ID #20033725, fetched directly from
    disclosures-clerk.house.gov during development). This is the actual
    pdfplumber-extracted text, boilerplate and all -- not a synthetic
    approximation. See CALIBRATION STATUS in congressional.py.
    """

    REAL_FILING_TEXT = """P T R
Clerk of the House of Representatives • Legislative Resource Center • B81 Cannon Building • Washington, DC 20515
F I
Name: Hon. Nancy Pelosi
Status: Member
State/District: CA11
T
ID Owner Asset Transaction
Type
Date Notification
Date Amount Cap.
Gains >
$200?
SP AllianceBernstein Holding L.P. Units
(AB) [AB]
P 01/16/2026 01/16/2026 $1,000,001 -
$5,000,000
F S: New
D: Purchased 25,000 shares.
SP Alphabet Inc. - Class A Common
Stock (GOOGL) [ST]
P 01/16/2026 01/16/2026 $500,001 -
$1,000,000
F S: New
D: Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $150 with an expiration date of
1/16/26.
SP Alphabet Inc. - Class A Common
Stock (GOOGL) [OP]
P 12/30/2025 12/30/2025 $250,001 -
$500,000
F S: New
D: Purchased 20 call options with a strike price of $150 and an expiration date of 1/15/27.
SP Alphabet Inc. - Class A Common
Stock (GOOGL) [ST]
S (partial) 12/30/2025 12/30/2025 $1,000,001 -
$5,000,000
F S: New
D: Contribution of 7,704 shares held personally to Donor-Advised Fund.
SP Amazon.com, Inc. - Common Stock
(AMZN) [OP]
P 12/30/2025 12/30/2025 $100,001 -
$250,000
F S: New
D: Purchased 20 call options with a strike price of $120 and an expiration date of 1/15/27.
SP Amazon.com, Inc. - Common Stock
(AMZN) [ST]
S (partial) 12/24/2025 12/24/2025 $1,000,001 -
$5,000,000
F S: New
Filing ID #20033725
ID Owner Asset Transaction
Type
Date Notification
Date Amount Cap.
Gains >
$200?
D: Sold 20,000 shares.
SP Amazon.com, Inc. - Common Stock
(AMZN) [ST]
P 01/16/2026 01/16/2026 $500,001 -
$1,000,000
F S: New
D: Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $150 with an expiration date of
1/16/26.
SP Apple Inc. - Common Stock (AAPL)
[ST]
S (partial) 12/24/2025 12/24/2025 $5,000,001 -
$25,000,000
F S: New
D: Sold 45,000 shares.
SP Apple Inc. - Common Stock (AAPL)
[OP]
P 12/30/2025 12/30/2025 $250,001 -
$500,000
F S: New
D: Purchased 20 call options with a strike price of $100 and an expiration date of 1/15/27.
SP Apple Inc. - Common Stock (AAPL)
[ST]
S (partial) 12/30/2025 12/30/2025 $5,000,001 -
$25,000,000
F S: New
D: Contribution of 28,200 shares to Donor-Advised Fund.
SP NVIDIA Corporation - Common Stock
(NVDA) [OP]
P 12/30/2025 12/30/2025 $100,001 -
$250,000
F S: New
D: Purchased 20 call options with a strike price of $100 and an expiration date of 1/15/27.
SP NVIDIA Corporation - Common Stock
(NVDA) [ST]
S (partial) 12/24/2025 12/24/2025 $1,000,001 -
$5,000,000
F S: New
D: Sold 20,000 shares.
SP NVIDIA Corporation - Common Stock
(NVDA) [ST]
P 01/16/2026 01/16/2026 $250,001 -
$500,000
F S: New
D: Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $80 with an expiration date of
1/16/26.
SP PayPal Holdings, Inc. - Common
Stock (PYPL) [ST]
S 12/30/2025 12/30/2025 $250,001 -
$500,000
F S: New
D: Sold 5,000 shares.
SP Tempus AI, Inc. - Class A Common P 01/16/2026 01/16/2026 $50,001 -
ID Owner Asset Transaction
Type
Date Notification
Date Amount Cap.
Gains >
$200?
Stock (TEM) [ST] $100,000
F S: New
D: Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $20 with an expiration date of
1/16/26.
SP Versant Media Group, Inc. - Class A
Common Stock (VSNT) [ST]
E 01/02/2026 01/02/2026 $15.00
F S: New
D: 776 shares and cash in lieu received as a result of spinoff from Comcast Corporation. No shares of Comcast
were surrendered as a result of the spinoff.
SP Vistra Corp. Common Stock (VST)
[ST]
P 01/16/2026 01/16/2026 $100,001 -
$250,000
F S: New
D: Exercised 50 call options purchased 1/14/25 (5,000 shares) at a strike price of $50 with an expiration date of
1/16/26.
SP Walt Disney Company (DIS) [ST] S 12/30/2025 12/30/2025 $1,000,001 -
$5,000,000
F S: New
D: Sold 10,000 shares.
* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.
I P O
 Yes No
C  S
 I CERTIFY that the statements I have made on the attached Periodic Transaction Report are true, complete, and correct to the best of
my knowledge and belief. Further, I CERTIFY that I have disclosed all transactions as required by the STOCK Act.
Digitally Signed: Hon. Nancy Pelosi , 01/23/2026
"""

    def test_parses_17_of_18_real_transactions(self):
        result = parse_house_ptr_text(self.REAL_FILING_TEXT, "20033725", "Hon. Nancy Pelosi")
        assert len(result.transactions) == 17

    def test_the_one_page_break_split_row_is_flagged_not_silently_dropped(self):
        # The Tempus AI (TEM) row is split across a PDF page boundary in
        # the real filing and cannot be reconstructed with confidence --
        # it must show up as flagged, not vanish and not get merged into
        # an adjacent transaction's fields.
        result = parse_house_ptr_text(self.REAL_FILING_TEXT, "20033725", "Hon. Nancy Pelosi")
        assert len(result.flagged) == 1
        assert "TEM" in result.flagged[0].raw_line

    def test_no_transaction_silently_absorbs_another_transactions_fields(self):
        # Regression guard for a real bug found during calibration: the
        # malformed TEM row was, before the asset_name restrictions were
        # added, silently swallowed into the NEXT transaction's asset_name
        # field (VSNT), losing the TEM transaction entirely without a flag.
        result = parse_house_ptr_text(self.REAL_FILING_TEXT, "20033725", "Hon. Nancy Pelosi")
        for t in result.transactions:
            assert "TEM" not in t.asset_name
            assert len(t.asset_name) < 100  # sanity bound -- no runaway match absorbing multiple rows

    def test_all_owner_codes_correctly_attributed_to_spouse(self):
        # Every transaction in this real filing is spousal (SP) -- if the
        # owner-code/asset-name boundary regex regresses, these would
        # silently default to SELF instead, which is a real correctness
        # bug (misattributes whose trade it was), not just cosmetic.
        result = parse_house_ptr_text(self.REAL_FILING_TEXT, "20033725", "Hon. Nancy Pelosi")
        assert all(t.owner == "SPOUSE" for t in result.transactions)
        assert all(not t.asset_name.startswith("SP ") for t in result.transactions)

    def test_known_tickers_and_amounts_parsed_correctly(self):
        result = parse_house_ptr_text(self.REAL_FILING_TEXT, "20033725", "Hon. Nancy Pelosi")
        by_ticker_and_date = {(t.ticker, t.transaction_date, t.transaction_type): t for t in result.transactions}

        ab = by_ticker_and_date[("AB", "2026-01-16", "purchase")]
        assert ab.amount_low == 1_000_001.0
        assert ab.amount_high == 5_000_000.0

        vsnt = by_ticker_and_date[("VSNT", "2026-01-02", "exchange")]
        assert vsnt.amount_low == 15.0
        assert vsnt.amount_high == 15.0

        dis = by_ticker_and_date[("DIS", "2025-12-30", "sale_full")]
        assert dis.amount_low == 1_000_001.0
        assert dis.amount_high == 5_000_000.0


def _senate_pdf_row(row_num: int, txn_date: str, owner: str, ticker: str, asset_lines: list[str], asset_type: str, txn_type_lines: list[str], amount_lines: list[str], comment: str = "--") -> str:
    """Builds a realistic multi-line Senate PTR row the way pdfplumber's
    extract_text() actually renders it -- the row number and date lead the
    row, the asset name commonly wraps onto its own line(s), the asset type
    and transaction type/amount can each wrap too (e.g. "Sale" then
    "(Partial)" on the next line), and a comment placeholder ("--" when
    there's no comment) follows. Mirrors the real Katie Britt PTR sample
    used in TestParseSenatePtrTextRealFiling below.
    """
    lines = [f"{row_num} {txn_date} {owner} {ticker} {asset_lines[0]}"]
    lines.extend(asset_lines[1:])
    lines.append(f"{asset_type} {txn_type_lines[0]}")
    lines.extend(txn_type_lines[1:])
    lines.append(amount_lines[0])
    lines.extend(amount_lines[1:])
    lines.append(comment)
    return "\n".join(lines)


class TestParseSenatePtrText:
    def test_parses_a_clean_sale_row(self):
        text = _senate_pdf_row(1, "01/15/2026", "Self", "AAPL", ["Apple Inc."], "Stock", ["Sale", "(Full)"], ["$1,001 -", "$15,000"])
        result = parse_senate_ptr_text(text, "efd-123", "Jane Doe")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.chamber == "senate"
        assert t.owner == "SELF"
        assert t.ticker == "AAPL"
        assert t.transaction_type == "sale_full"
        assert t.transaction_date == "2026-01-15"
        assert result.flagged == []

    def test_parses_spouse_purchase(self):
        text = _senate_pdf_row(2, "03/02/2026", "Spouse", "MSFT", ["Microsoft Corp"], "Stock", ["Purchase"], ["$15,001 -", "$50,000"])
        result = parse_senate_ptr_text(text, "efd-124", "Jane Doe")
        t = result.transactions[0]
        assert t.owner == "SPOUSE"
        assert t.transaction_type == "purchase"

    def test_unmatched_asset_type_line_is_flagged(self):
        # "Weird Category" isn't a valid asset type (Stock/Bond/Fund/Option/Other)
        text = "1 01/15/2026 Self AAPL Apple Inc. Weird Category Purchase $1,001 - $15,000"
        result = parse_senate_ptr_text(text, "efd-125", "Jane Doe")
        assert result.transactions == []
        assert len(result.flagged) == 1

    def test_non_row_lines_ignored(self):
        text = "\n".join(["ANNUAL REPORT", "Name: Jane Doe", ""])
        result = parse_senate_ptr_text(text, "efd-126", "Jane Doe")
        assert result.transactions == []
        assert result.flagged == []

    def test_boilerplate_lines_are_ignored_not_flagged(self):
        text = "\n".join(
            [
                "United States Senate",
                "Financial Disclosures",
                "Periodic Transaction Report for 01/26/2026",
                "Mrs. Jane Doe (Doe, Jane)",
                " Filed 01/26/2026 @ 5:46 PM",
                "The following statements were checked before filing:",
                "I certify that the statements I have made on this form are true, complete and correct to the best of",
                "my knowledge and belief.",
                "I understand that reports cannot be edited once filed. To make corrections, I will submit an",
                "electronic amendment to this report.",
                " Transactions (0 transactions total) 0 Self 0 Joint 0 Spouse 0 Dependent Child",
                "#",
                "Transaction Date Owner Ticker Asset Name",
                "Asset",
                "Type Type Amount Comment",
            ]
        )
        result = parse_senate_ptr_text(text, "efd-127", "Jane Doe")
        assert result.transactions == []
        assert result.flagged == []

    def test_multiple_rows_mixed_clean_and_flagged(self):
        good1 = _senate_pdf_row(3, "01/15/2026", "Self", "AAPL", ["Apple Inc."], "Stock", ["Purchase"], ["$1,001 -", "$15,000"])
        good2 = _senate_pdf_row(2, "03/02/2026", "Spouse", "MSFT", ["Microsoft Corp"], "Stock", ["Sale", "(Partial)"], ["$15,001 -", "$50,000"])
        bad = "1 01/01/2026 Self XX Some Garbage Corp Weird Category Purchase $5.00"
        text = "\n".join([good1, bad, good2])
        result = parse_senate_ptr_text(text, "efd-128", "Jane Doe")
        assert len(result.transactions) == 2
        assert len(result.flagged) == 1


class TestParseSenatePtrTextRealFiling:
    """Validated against a real, live-fetched Senate PTR filing (Sen. Katie
    Britt, filed 01/26/2026, fetched via a public mirror of the PDF served
    by efdsearch.senate.gov -- efdsearch itself requires an interactive
    terms-acceptance session that couldn't be completed from this
    environment, but the underlying PDF and its pdfplumber-style extracted
    text are real). This is the actual extracted text, boilerplate, row
    numbers, and line-wraps included -- not a synthetic approximation. See
    CALIBRATION STATUS in congressional.py.
    """

    REAL_FILING_TEXT = """United States Senate
Financial Disclosures
Periodic Transaction Report for 01/26/2026
Mrs. Katie Britt (Britt, Katie)
 Filed 01/26/2026 @ 5:46 PM
The following statements were checked before filing:
I certify that the statements I have made on this form are true, complete and correct to the best of
my knowledge and belief.
I understand that reports cannot be edited once filed. To make corrections, I will submit an
electronic amendment to this report.
 Transactions (22 transactions total) 0 Self 0 Joint 22 Spouse 0 Dependent Child
#
Transaction Date Owner Ticker Asset Name
Asset
Type Type Amount Comment
22 04/14/2025 Spouse XOM Exxon Mobil
Corp
Stock Purchase $1,001 -
$15,000
--
21 04/14/2025 Spouse JPM JP Morgan
Chase &
Company
Stock Purchase $1,001 -
$15,000
--
20 04/14/2025 Spouse GOOG Alphabet Cl
C
Stock Purchase $1,001 -
$15,000
--
19 04/14/2025 Spouse EOG EOG
Resources,
Inc.
Common
Stock
Stock Purchase $1,001 -
$15,000
--
18 04/14/2025 Spouse AAPL Apple Inc Stock Purchase $1,001 -
$15,000
--
17 04/14/2025 Spouse AMZN Amazon.com
Inc
Stock Purchase $1,001 -
$15,000
--
16 04/14/2025 Spouse WMT Walmart Inc Stock Purchase $1,001 -
$15,000
--
15 04/14/2025 Spouse V Visa Inc Stock Purchase $1,001 -
$15,000
--
14 04/14/2025 Spouse MSFT Microsoft
Corp
Stock Purchase $1,001 -
$15,000
--
13 04/14/2025 Spouse UNH Unitedhealth
Group Inc
Stock Purchase $1,001 -
$15,000
--
12 04/14/2025 Spouse NVDA Nvidia Corp Stock Purchase $1,001 -
$15,000
--
11 04/30/2025 Spouse Amazon.com Stock Purchase $1,001 - --
AMZN Inc $15,000
10 04/30/2025 Spouse AAPL Apple Inc Stock Purchase $1,001 -
$15,000
--
9 04/30/2025 Spouse UNH Unitedhealth
Group Inc
Stock Sale
(Full)
$1,001 -
$15,000
--
8 04/30/2025 Spouse NVDA Nvidia Corp Stock Purchase $1,001 -
$15,000
--
7 04/30/2025 Spouse XOM Exxon Mobil
Corp
Stock Sale
(Full)
$1,001 -
$15,000
--
6 11/07/2025 Spouse NVDA Nvidia Corp Stock Sale
(Partial)
$1,001 -
$15,000
--
5 11/07/2025 Spouse AAPL Apple Inc Stock Sale
(Partial)
$1,001 -
$15,000
--
4 11/07/2025 Spouse GOOG Alphabet Cl
C
Stock Sale
(Partial)
$1,001 -
$15,000
--
3 11/07/2025 Spouse UPS United
Parcel
Service
Stock Purchase $1,001 -
$15,000
--
2 11/07/2025 Spouse AMZN Amazon.com
Inc
Stock Sale
(Partial)
$1,001 -
$15,000
--
1 11/07/2025 Spouse V Visa Inc Stock Purchase $1,001 -
$15,000
--
"""

    def test_parses_21_of_22_real_transactions(self):
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        assert len(result.transactions) == 21

    def test_the_one_page_break_split_row_is_flagged_not_silently_dropped(self):
        # Transaction #11 (an AMZN purchase) has its ticker land after the
        # comment placeholder on a wrapped line in the real extracted text
        # -- it must show up as flagged, not vanish and not get merged into
        # an adjacent transaction's fields.
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        assert len(result.flagged) == 1
        assert "11" in result.flagged[0].raw_line
        assert "04/30/2025" in result.flagged[0].raw_line

    def test_no_transaction_silently_absorbs_another_transactions_fields(self):
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        for t in result.transactions:
            assert len(t.asset_name) < 60  # sanity bound -- no runaway match absorbing multiple rows

    def test_all_owner_codes_correctly_attributed_to_spouse(self):
        # Every transaction in this real filing is spousal -- if the
        # boundary regex regresses, these would silently default to
        # something else, which is a real correctness bug, not cosmetic.
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        assert all(t.owner == "SPOUSE" for t in result.transactions)

    def test_known_tickers_and_amounts_parsed_correctly(self):
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        by_ticker_and_date = {(t.ticker, t.transaction_date, t.transaction_type): t for t in result.transactions}

        xom = by_ticker_and_date[("XOM", "2025-04-14", "purchase")]
        assert xom.amount_low == 1_001.0
        assert xom.amount_high == 15_000.0

        unh_sale = by_ticker_and_date[("UNH", "2025-04-30", "sale_full")]
        assert unh_sale.amount_low == 1_001.0

        nvda_partial = by_ticker_and_date[("NVDA", "2025-11-07", "sale_partial")]
        assert nvda_partial.amount_high == 15_000.0

    def test_wrapped_asset_name_containing_the_word_stock_still_parses(self):
        # EOG's asset name is "EOG Resources, Inc. Common Stock" -- the
        # literal word "Stock" appearing inside the asset name (before the
        # real Asset Type column, which is also "Stock") is a real
        # ambiguity in this filing and a good regression guard for the
        # asset_name/asset_type boundary.
        result = parse_senate_ptr_text(self.REAL_FILING_TEXT, "britt-2026-01-26", "Katie Britt")
        eog = next(t for t in result.transactions if t.ticker == "EOG")
        assert eog.asset_name == "EOG Resources, Inc. Common Stock"


class TestParsePtrTextDispatch:
    def test_dispatches_to_house(self):
        text = "Apple Inc. (AAPL) [ST] P 01/15/2026 02/03/2026 $1,001 - $15,000"
        result = parse_ptr_text(text, "house", "doc1", "SMITH JOHN")
        assert result.transactions[0].chamber == "house"

    def test_dispatches_to_senate(self):
        text = "1 01/15/2026 Self AAPL Apple Inc. Stock Purchase $1,001 - $15,000"
        result = parse_ptr_text(text, "senate", "doc2", "Jane Doe")
        assert result.transactions[0].chamber == "senate"

    def test_unknown_chamber_raises(self):
        with pytest.raises(ValueError):
            parse_ptr_text("anything", "house_of_lords", "doc3", "Someone")


class TestFilterFilingsByDate:
    def test_keeps_filings_on_or_after_since(self):
        filings = [{"docId": "1", "filingDate": "01/15/2026"}, {"docId": "2", "filingDate": "01/20/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 18))
        assert [f["docId"] for f in result] == ["2"]

    def test_since_boundary_is_inclusive(self):
        filings = [{"docId": "1", "filingDate": "01/18/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 18))
        assert len(result) == 1

    def test_until_excludes_later_filings(self):
        filings = [{"docId": "1", "filingDate": "01/15/2026"}, {"docId": "2", "filingDate": "01/25/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 1), until=date(2026, 1, 20))
        assert [f["docId"] for f in result] == ["1"]

    def test_until_boundary_is_inclusive(self):
        filings = [{"docId": "1", "filingDate": "01/20/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 1), until=date(2026, 1, 20))
        assert len(result) == 1

    def test_missing_filing_date_is_skipped_not_raised(self):
        filings = [{"docId": "1"}, {"docId": "2", "filingDate": "01/20/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 1))
        assert [f["docId"] for f in result] == ["2"]

    def test_unparseable_filing_date_is_skipped_not_raised(self):
        filings = [{"docId": "1", "filingDate": "not-a-date"}, {"docId": "2", "filingDate": "01/20/2026"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 1))
        assert [f["docId"] for f in result] == ["2"]

    def test_no_until_means_unbounded_upper_end(self):
        filings = [{"docId": "1", "filingDate": "12/31/2099"}]
        result = filter_filings_by_date(filings, since=date(2026, 1, 1))
        assert len(result) == 1

    def test_empty_input_returns_empty_list(self):
        assert filter_filings_by_date([], since=date(2026, 1, 1)) == []


class TestAggregateTransactionsByTicker:
    def _txn(self, ticker: str) -> CongressionalTransaction:
        return CongressionalTransaction(
            chamber="house",
            source_doc_id="doc1",
            filer_name="SMITH JOHN",
            owner="SELF",
            ticker=ticker,
            asset_name="Some Asset",
            transaction_type="purchase",
            transaction_date="2026-01-15",
            amount_low=1001.0,
            amount_high=15000.0,
            raw_line="raw",
        )

    def test_buckets_by_ticker(self):
        txns = [self._txn("AAPL"), self._txn("AAPL"), self._txn("MSFT")]
        result = aggregate_transactions_by_ticker(txns)
        assert len(result["AAPL"]) == 2
        assert len(result["MSFT"]) == 1

    def test_empty_input_returns_empty_dict(self):
        assert aggregate_transactions_by_ticker([]) == {}


class TestComputeCongressionalSignal:
    def _txn(self, **overrides) -> CongressionalTransaction:
        defaults = dict(
            chamber="house",
            source_doc_id="doc1",
            filer_name="SMITH JOHN",
            owner="SELF",
            ticker="AAPL",
            asset_name="Apple Inc.",
            transaction_type="purchase",
            transaction_date="2026-01-15",
            amount_low=1001.0,
            amount_high=15000.0,
            raw_line="raw",
        )
        defaults.update(overrides)
        return CongressionalTransaction(**defaults)

    def test_net_buying_is_bullish(self):
        result = compute_congressional_signal([self._txn(transaction_type="purchase")])
        assert result.score > 0
        assert result.num_buy_transactions == 1

    def test_net_selling_is_bearish(self):
        result = compute_congressional_signal([self._txn(transaction_type="sale_full")])
        assert result.score < 0
        assert result.num_sell_transactions == 1

    def test_empty_is_neutral(self):
        result = compute_congressional_signal([])
        assert result.score == 0.0

    def test_exchange_does_not_affect_dollar_math(self):
        result = compute_congressional_signal([self._txn(transaction_type="exchange")])
        assert result.net_dollar_value_midpoint == 0.0

    def test_uses_bracket_midpoint(self):
        result = compute_congressional_signal(
            [self._txn(amount_low=1000.0, amount_high=2000.0, transaction_type="purchase")]
        )
        assert result.net_dollar_value_midpoint == pytest.approx(1500.0)

    def test_open_ended_bracket_uses_multiplier(self):
        config = CongressionalSignalConfig(amount_midpoint_for_open_ended=2.0, score_cap_dollars=1_000_000)
        result = compute_congressional_signal(
            [self._txn(amount_low=50_000_000.0, amount_high=None, transaction_type="purchase")],
            config=config,
        )
        assert result.net_dollar_value_midpoint == pytest.approx(100_000_000.0)
        assert result.score == pytest.approx(100.0)  # clipped

    def test_score_is_capped(self):
        config = CongressionalSignalConfig(score_cap_dollars=1_000.0)
        result = compute_congressional_signal(
            [self._txn(amount_low=1000.0, amount_high=2000.0, transaction_type="purchase")],
            config=config,
        )
        assert result.score == pytest.approx(100.0)

    def test_flagged_count_surfaced_in_reasoning(self):
        from src.signals.congressional import FlaggedLine

        flagged = [FlaggedLine(chamber="house", source_doc_id="doc1", raw_line="x", reason="bad")]
        result = compute_congressional_signal([self._txn()], flagged=flagged)
        assert result.num_flagged == 1
        assert "skipped as unparseable" in result.reasoning

    def test_multiple_transactions_net_out(self):
        result = compute_congressional_signal(
            [
                self._txn(transaction_type="purchase", amount_low=1000.0, amount_high=2000.0),
                self._txn(transaction_type="sale_full", amount_low=1000.0, amount_high=2000.0),
            ]
        )
        assert result.net_dollar_value_midpoint == pytest.approx(0.0)
        assert result.num_buy_transactions == 1
        assert result.num_sell_transactions == 1


class TestParseResultCacheSerialization:
    """The per-docId parse cache (added 2026-07-21 alongside the 30-day
    congressional lookback window) round-trips ParseResult through a
    JSON-safe dict. A stale/corrupt/old-format payload must deserialize to
    None (meaning "refetch"), never crash or produce a wrong ParseResult."""

    def _result(self) -> ParseResult:
        return ParseResult(
            transactions=[
                CongressionalTransaction(
                    chamber="house",
                    source_doc_id="20033725",
                    filer_name="PELOSI NANCY",
                    owner="SPOUSE",
                    ticker="AAPL",
                    asset_name="Apple Inc.",
                    transaction_type="purchase",
                    transaction_date="2026-06-15",
                    amount_low=1001.0,
                    amount_high=15000.0,
                    raw_line="raw text",
                ),
                CongressionalTransaction(
                    chamber="house",
                    source_doc_id="20033725",
                    filer_name="PELOSI NANCY",
                    owner="SELF",
                    ticker="NVDA",
                    asset_name="NVIDIA Corp",
                    transaction_type="sale_partial",
                    transaction_date="2026-06-20",
                    amount_low=50000.0,
                    amount_high=None,  # open-ended bracket survives round-trip
                    raw_line="raw text 2",
                ),
            ],
            flagged=[
                FlaggedLine(
                    chamber="house",
                    source_doc_id="20033725",
                    raw_line="mangled row",
                    reason="page-break split",
                )
            ],
        )

    def test_round_trip_preserves_everything(self):
        original = self._result()
        restored = parse_result_from_cache_dict(parse_result_to_cache_dict(original))
        assert restored == original

    def test_dict_is_json_safe(self):
        import json

        payload = parse_result_to_cache_dict(self._result())
        restored = parse_result_from_cache_dict(json.loads(json.dumps(payload)))
        assert restored == self._result()

    def test_empty_result_round_trips(self):
        restored = parse_result_from_cache_dict(parse_result_to_cache_dict(ParseResult()))
        assert restored == ParseResult()

    def test_wrong_version_returns_none(self):
        payload = parse_result_to_cache_dict(self._result())
        payload["version"] = 999
        assert parse_result_from_cache_dict(payload) is None

    def test_non_dict_payload_returns_none(self):
        assert parse_result_from_cache_dict(None) is None
        assert parse_result_from_cache_dict([1, 2]) is None
        assert parse_result_from_cache_dict("junk") is None

    def test_malformed_fields_return_none(self):
        payload = parse_result_to_cache_dict(self._result())
        payload["transactions"][0].pop("ticker")
        assert parse_result_from_cache_dict(payload) is None

    def test_unexpected_field_returns_none(self):
        payload = parse_result_to_cache_dict(self._result())
        payload["flagged"][0]["surprise"] = True
        assert parse_result_from_cache_dict(payload) is None

    def test_disk_store_and_load_round_trip(self, tmp_path):
        from src.signals.congressional import _load_cached_parse_result, _store_cached_parse_result

        original = self._result()
        _store_cached_parse_result(tmp_path, "20033725", original)
        assert _load_cached_parse_result(tmp_path, "20033725") == original

    def test_disk_load_missing_or_corrupt_returns_none(self, tmp_path):
        from src.signals.congressional import _load_cached_parse_result

        assert _load_cached_parse_result(tmp_path, "nope") is None
        (tmp_path / "bad.json").write_text("{not json")
        assert _load_cached_parse_result(tmp_path, "bad") is None

    def test_disk_helpers_refuse_unsafe_doc_ids(self, tmp_path):
        # A docId is used as a filename -- anything that could escape the
        # cache dir (path separators, "..") must be refused outright.
        from src.signals.congressional import _load_cached_parse_result, _store_cached_parse_result

        _store_cached_parse_result(tmp_path, "../escape", self._result())
        assert not (tmp_path.parent / "escape.json").exists()
        assert _load_cached_parse_result(tmp_path, "../escape") is None

    def test_no_cache_dir_is_a_noop(self):
        from src.signals.congressional import _load_cached_parse_result, _store_cached_parse_result

        _store_cached_parse_result(None, "20033725", self._result())
        assert _load_cached_parse_result(None, "20033725") is None
