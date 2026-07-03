import pytest

from src.signals.insider_edgar import (
    InsiderSignalConfig,
    InsiderTransaction,
    compute_insider_signal,
    parse_form4_xml,
)


def make_form4_xml(
    *,
    transaction_code: str = "S",
    acquired_disposed: str = "D",
    shares: str = "50000",
    price: str = "195.23",
    is_officer: str = "1",
    is_director: str = "1",
    is_ten_percent_owner: str = "0",
    include_price: bool = True,
    include_shares: bool = True,
) -> str:
    price_block = f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>" if include_price else ""
    shares_block = f"<transactionShares><value>{shares}</value></transactionShares>" if include_shares else ""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0508</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-06-15</periodOfReport>
    <issuer>
        <issuerCik>0000320193</issuerCik>
        <issuerName>Example Corp</issuerName>
        <issuerTradingSymbol>EXMPL</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001214156</rptOwnerCik>
            <rptOwnerName>DOE JANE</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>{is_director}</isDirector>
            <isOfficer>{is_officer}</isOfficer>
            <isTenPercentOwner>{is_ten_percent_owner}</isTenPercentOwner>
            <officerTitle>Chief Executive Officer</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-06-15</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>{transaction_code}</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
            </transactionCoding>
            <transactionAmounts>
                {shares_block}
                {price_block}
                <transactionAcquiredDisposedCode><value>{acquired_disposed}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>3200000</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


class TestParseForm4Xml:
    def test_parses_a_sale_transaction(self):
        xml = make_form4_xml(transaction_code="S", acquired_disposed="D", shares="50000", price="195.23")
        transactions = parse_form4_xml(xml, "EXMPL")
        assert len(transactions) == 1
        t = transactions[0]
        assert t.symbol == "EXMPL"
        assert t.filer_name == "DOE JANE"
        assert t.filer_cik == "0001214156"
        assert t.is_officer is True
        assert t.is_director is True
        assert t.is_ten_percent_owner is False
        assert t.transaction_code == "S"
        assert t.acquired_disposed == "D"
        assert t.shares == 50000.0
        assert t.price_per_share == 195.23
        assert t.shares_owned_after == 3200000.0

    def test_parses_a_purchase_transaction(self):
        xml = make_form4_xml(transaction_code="P", acquired_disposed="A", shares="1000", price="10.50")
        transactions = parse_form4_xml(xml, "EXMPL")
        assert transactions[0].acquired_disposed == "A"
        assert transactions[0].transaction_code == "P"

    def test_ten_percent_owner_flag_parsed(self):
        xml = make_form4_xml(is_officer="0", is_director="0", is_ten_percent_owner="1")
        t = parse_form4_xml(xml, "EXMPL")[0]
        assert t.is_officer is False
        assert t.is_director is False
        assert t.is_ten_percent_owner is True

    def test_malformed_xml_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_form4_xml("<not valid xml", "EXMPL")

    def test_missing_reporting_owner_raises(self):
        xml = """<ownershipDocument><issuer><issuerName>X</issuerName></issuer></ownershipDocument>"""
        with pytest.raises(ValueError):
            parse_form4_xml(xml, "EXMPL")

    def test_no_non_derivative_table_returns_empty_list_not_error(self):
        xml = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001214156</rptOwnerCik>
            <rptOwnerName>DOE JANE</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
        </reportingOwnerRelationship>
    </reportingOwner>
</ownershipDocument>"""
        assert parse_form4_xml(xml, "EXMPL") == []

    def test_transaction_missing_shares_is_skipped_not_guessed(self):
        xml = make_form4_xml(include_shares=False)
        assert parse_form4_xml(xml, "EXMPL") == []

    def test_transaction_missing_price_keeps_transaction_with_none_price(self):
        xml = make_form4_xml(include_price=False)
        transactions = parse_form4_xml(xml, "EXMPL")
        assert len(transactions) == 1
        assert transactions[0].price_per_share is None


class TestComputeInsiderSignal:
    def _txn(self, **overrides) -> InsiderTransaction:
        defaults = dict(
            symbol="EXMPL",
            filer_cik="0001214156",
            filer_name="DOE JANE",
            is_officer=True,
            is_director=True,
            is_ten_percent_owner=False,
            transaction_date="2026-06-15",
            transaction_code="P",
            acquired_disposed="A",
            shares=1000.0,
            price_per_share=100.0,
            shares_owned_after=5000.0,
        )
        defaults.update(overrides)
        return InsiderTransaction(**defaults)

    def test_net_buying_is_bullish(self):
        result = compute_insider_signal([self._txn(transaction_code="P", acquired_disposed="A")])
        assert result.score > 0
        assert result.num_buy_transactions == 1
        assert result.num_sell_transactions == 0

    def test_net_selling_is_bearish(self):
        result = compute_insider_signal(
            [self._txn(transaction_code="S", acquired_disposed="D")]
        )
        assert result.score < 0
        assert result.num_sell_transactions == 1

    def test_empty_list_is_neutral(self):
        result = compute_insider_signal([])
        assert result.score == 0.0
        assert result.num_transactions_considered == 0

    def test_only_non_open_market_codes_is_neutral(self):
        # Grants (A) and tax withholding (F) shouldn't move the signal
        result = compute_insider_signal(
            [self._txn(transaction_code="A", acquired_disposed="A"), self._txn(transaction_code="F", acquired_disposed="D")]
        )
        assert result.score == 0.0
        assert result.num_transactions_considered == 0

    def test_officer_director_weighted_higher_than_other(self):
        officer_result = compute_insider_signal(
            [self._txn(is_officer=True, is_director=True, is_ten_percent_owner=False)]
        )
        other_result = compute_insider_signal(
            [self._txn(is_officer=False, is_director=False, is_ten_percent_owner=False)]
        )
        assert officer_result.score > other_result.score

    def test_score_is_capped_at_100(self):
        config = InsiderSignalConfig(score_cap_dollars=1_000.0)
        result = compute_insider_signal(
            [self._txn(shares=1_000_000.0, price_per_share=500.0)], config=config
        )
        assert result.score == pytest.approx(100.0)

    def test_score_is_capped_at_negative_100(self):
        config = InsiderSignalConfig(score_cap_dollars=1_000.0)
        result = compute_insider_signal(
            [self._txn(shares=1_000_000.0, price_per_share=500.0, transaction_code="S", acquired_disposed="D")],
            config=config,
        )
        assert result.score == pytest.approx(-100.0)

    def test_transactions_missing_price_are_skipped_in_dollar_math(self):
        result = compute_insider_signal([self._txn(price_per_share=None)])
        assert result.net_weighted_dollar_value == 0.0

    def test_negative_shares_raises(self):
        with pytest.raises(ValueError):
            compute_insider_signal([self._txn(shares=-100.0)])

    def test_invalid_acquired_disposed_code_raises(self):
        with pytest.raises(ValueError):
            compute_insider_signal([self._txn(acquired_disposed="X")])

    def test_multiple_transactions_net_out(self):
        result = compute_insider_signal(
            [
                self._txn(transaction_code="P", acquired_disposed="A", shares=1000, price_per_share=100.0),
                self._txn(transaction_code="S", acquired_disposed="D", shares=400, price_per_share=100.0),
            ]
        )
        # net buy of 600 shares * 100 * 1.5 weight = 90,000
        assert result.net_weighted_dollar_value == pytest.approx(90_000.0)
        assert result.num_buy_transactions == 1
        assert result.num_sell_transactions == 1
