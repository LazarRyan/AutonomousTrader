import pytest

from src.signals.congressional import (
    CongressionalSignalConfig,
    CongressionalTransaction,
    compute_congressional_signal,
    parse_house_ptr_text,
    parse_ptr_text,
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


class TestParseSenatePtrText:
    def test_parses_a_clean_sale_row(self):
        text = "01/15/2026 Self AAPL Apple Inc. Stock Sale (Full) $1,001 - $15,000"
        result = parse_senate_ptr_text(text, "efd-123", "Jane Doe")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.chamber == "senate"
        assert t.owner == "SELF"
        assert t.ticker == "AAPL"
        assert t.transaction_type == "sale_full"
        assert t.transaction_date == "2026-01-15"

    def test_parses_spouse_purchase(self):
        text = "03/02/2026 Spouse MSFT Microsoft Corp Stock Purchase $15,001 - $50,000"
        result = parse_senate_ptr_text(text, "efd-124", "Jane Doe")
        t = result.transactions[0]
        assert t.owner == "SPOUSE"
        assert t.transaction_type == "purchase"

    def test_unmatched_date_led_line_is_flagged(self):
        text = "01/15/2026 Self AAPL Apple Inc. Weird Category Purchase $1,001 - $15,000"
        result = parse_senate_ptr_text(text, "efd-125", "Jane Doe")
        assert result.transactions == []
        assert len(result.flagged) == 1

    def test_non_row_lines_ignored(self):
        text = "\n".join(["ANNUAL REPORT", "Name: Jane Doe", ""])
        result = parse_senate_ptr_text(text, "efd-126", "Jane Doe")
        assert result.transactions == []
        assert result.flagged == []


class TestParsePtrTextDispatch:
    def test_dispatches_to_house(self):
        text = "Apple Inc. (AAPL) [ST] P 01/15/2026 02/03/2026 $1,001 - $15,000"
        result = parse_ptr_text(text, "house", "doc1", "SMITH JOHN")
        assert result.transactions[0].chamber == "house"

    def test_dispatches_to_senate(self):
        text = "01/15/2026 Self AAPL Apple Inc. Stock Purchase $1,001 - $15,000"
        result = parse_ptr_text(text, "senate", "doc2", "Jane Doe")
        assert result.transactions[0].chamber == "senate"

    def test_unknown_chamber_raises(self):
        with pytest.raises(ValueError):
            parse_ptr_text("anything", "house_of_lords", "doc3", "Someone")


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
