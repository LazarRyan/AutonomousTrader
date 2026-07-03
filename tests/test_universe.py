from src.universe import load_sp500_cik_map, load_sp500_universe


class TestLoadSp500Universe:
    def test_loads_a_large_list_of_tickers(self):
        universe = load_sp500_universe()
        assert len(universe) > 490  # S&P 500 constituent count fluctuates slightly
        assert "AAPL" in universe
        assert "MSFT" in universe

    def test_no_duplicate_tickers(self):
        universe = load_sp500_universe()
        assert len(universe) == len(set(universe))

    def test_all_tickers_look_like_tickers(self):
        universe = load_sp500_universe()
        for symbol in universe:
            assert symbol.strip() == symbol
            assert len(symbol) > 0


class TestLoadSp500CikMap:
    def test_returns_cik_for_known_ticker(self):
        cik_map = load_sp500_cik_map()
        assert cik_map["AAPL"] == "0000320193"

    def test_cik_is_zero_padded_ten_digits(self):
        cik_map = load_sp500_cik_map()
        for symbol, cik in cik_map.items():
            assert len(cik) == 10, f"{symbol} has non-10-digit CIK: {cik!r}"
            assert cik.isdigit()

    def test_same_length_as_universe(self):
        assert len(load_sp500_cik_map()) == len(load_sp500_universe())
