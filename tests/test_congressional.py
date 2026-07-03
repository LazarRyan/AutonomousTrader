import pytest

from src.signals.congressional import (
    CongressionalSignalConfig,
    CongressionalTransaction,
    compute_congressional_signal,
    parse_house_ptr_text,
    parse_ptr_text,
    parse_senate_ptr_text,
)


class TestParseHousePtrText:
    def test_parses_a_clean_purchase_row(self):
        text = "Apple Inc. (AAPL) [ST] P 01/15/2026 02/03/2026 $1,001 - $15,000"
        result = parse_house_ptr_text(text, "20033725", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.chamber == "house"
        assert t.ticker == "AAPL"
        assert t.asset_name == "Apple Inc."
        assert t.transaction_type == "purchase"
        assert t.transaction_date == "2026-01-15"
        assert t.amount_low == 1001.0
        assert t.amount_high == 15000.0
        assert t.owner == "SELF"
        assert result.flagged == []

    def test_parses_owner_code_prefix(self):
        text = "SP Microsoft Corp (MSFT) [ST] S (partial) 03/02/2026 03/20/2026 $15,001 - $50,000"
        result = parse_house_ptr_text(text, "20033726", "SMITH JOHN")
        t = result.transactions[0]
        assert t.owner == "SPOUSE"
        assert t.transaction_type == "sale_partial"
        assert t.ticker == "MSFT"

    def test_parses_dotted_ticker_and_over_bracket_amount(self):
        text = "Berkshire Hathaway (BRK.A) [ST] P 01/01/2026 01/20/2026 Over $50,000,000"
        result = parse_house_ptr_text(text, "20033727", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.ticker == "BRK.A"
        assert t.amount_low == 50_000_000.0
        assert t.amount_high is None
        assert result.flagged == []

    def test_parses_over_bracket_amount_with_clean_ticker(self):
        text = "Example Fund (EXFD) [ST] P 01/01/2026 01/20/2026 Over $50,000,000"
        result = parse_house_ptr_text(text, "20033728", "SMITH JOHN")
        assert len(result.transactions) == 1
        t = result.transactions[0]
        assert t.amount_low == 50_000_000.0
        assert t.amount_high is None

    def test_boilerplate_lines_are_ignored_not_flagged(self):
        text = "\n".join(
            [
                "PERIODIC TRANSACTION REPORT",
                "Name: John Smith",
                "Page 1 of 3",
                "I certify the above is true and correct.",
            ]
        )
        result = parse_house_ptr_text(text, "20033729", "SMITH JOHN")
        assert result.transactions == []
        assert result.flagged == []  # these lines never looked like transaction rows

    def test_row_that_looks_like_transaction_but_malformed_is_flagged(self):
        text = "Apple Inc. (AAPL) [ST] Q 01/15/2026 02/03/2026 $1,001 - $15,000"  # "Q" isn't a valid txn type
        result = parse_house_ptr_text(text, "20033730", "SMITH JOHN")
        assert result.transactions == []
        assert len(result.flagged) == 1
        assert "did not match" in result.flagged[0].reason

    def test_multiple_rows_mixed_clean_and_flagged(self):
        text = "\n".join(
            [
                "Apple Inc. (AAPL) [ST] P 01/15/2026 02/03/2026 $1,001 - $15,000",
                "Some Garbage Row (XX) $5 not a real format",
                "Microsoft Corp (MSFT) [ST] S 03/02/2026 03/20/2026 $15,001 - $50,000",
            ]
        )
        result = parse_house_ptr_text(text, "20033731", "SMITH JOHN")
        assert len(result.transactions) == 2
        assert len(result.flagged) == 1


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
