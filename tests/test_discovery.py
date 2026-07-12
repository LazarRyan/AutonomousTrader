from datetime import datetime, time as dtime, timezone

from src.discovery import DailySchedule, compute_lookback_start, rank_discovered_symbols


class TestRankDiscoveredSymbols:
    def test_ranks_by_mention_count_descending(self):
        mentions = [["AAPL", "MSFT"], ["AAPL"], ["AAPL"], ["MSFT"]]
        result = rank_discovered_symbols(mentions)
        assert result == ["AAPL", "MSFT"]

    def test_ties_broken_alphabetically(self):
        mentions = [["MSFT"], ["AAPL"]]
        result = rank_discovered_symbols(mentions)
        assert result == ["AAPL", "MSFT"]

    def test_cap_limits_result_size(self):
        mentions = [["AAPL"], ["MSFT"], ["GOOGL"]]
        result = rank_discovered_symbols(mentions, cap=2)
        assert result == ["AAPL", "GOOGL"]  # top 2 alphabetically among 1-mention ties

    def test_valid_universe_filters_out_unknown_symbols(self):
        mentions = [["AAPL"], ["XYZJUNK"]]
        result = rank_discovered_symbols(mentions, valid_universe={"AAPL", "MSFT"})
        assert result == ["AAPL"]

    def test_blank_and_whitespace_symbols_ignored(self):
        mentions = [["AAPL"], [""], ["  "]]
        result = rank_discovered_symbols(mentions)
        assert result == ["AAPL"]

    def test_symbols_normalized_to_uppercase_and_stripped(self):
        mentions = [[" aapl "], ["AAPL"]]
        result = rank_discovered_symbols(mentions)
        assert result == ["AAPL"]

    def test_empty_input_returns_empty_list(self):
        assert rank_discovered_symbols([]) == []


class TestComputeLookbackStart:
    ET = timezone.utc  # tests use a fixed offset stand-in; real caller passes exchange-local tz

    def _dt(self, hour: int, minute: int) -> datetime:
        return datetime(2026, 7, 6, hour, minute, tzinfo=self.ET)  # a Monday

    def test_first_slot_of_day_looks_back_to_previous_session_close(self):
        previous_close = datetime(2026, 7, 2, 16, 0, tzinfo=self.ET)  # previous Thursday close
        now = self._dt(9, 21)  # just after the 9:20 slot fires
        result = compute_lookback_start(now, previous_close)
        assert result == previous_close

    def test_second_slot_looks_back_to_first_slot_same_day(self):
        previous_close = datetime(2026, 7, 6, 9, 30, tzinfo=self.ET)  # today's open, irrelevant here
        now = self._dt(13, 31)  # just after the 1:30pm slot fires
        result = compute_lookback_start(now, previous_close)
        assert result == self._dt(9, 20)

    def test_third_slot_looks_back_to_second_slot_same_day(self):
        previous_close = self._dt(9, 30)
        now = self._dt(15, 51)  # just after the 3:50pm slot fires
        result = compute_lookback_start(now, previous_close)
        assert result == self._dt(13, 30)

    def test_invoked_before_any_scheduled_slot_looks_back_to_previous_close(self):
        # e.g. a manual ad-hoc dry run at 8am, before the first real slot.
        previous_close = datetime(2026, 7, 2, 16, 0, tzinfo=self.ET)
        now = self._dt(8, 0)
        result = compute_lookback_start(now, previous_close)
        assert result == previous_close

    def test_custom_schedule_respected(self):
        schedule = DailySchedule(slot_times=(dtime(10, 0), dtime(15, 0)))
        previous_close = datetime(2026, 7, 2, 16, 0, tzinfo=self.ET)
        now = self._dt(15, 1)
        result = compute_lookback_start(now, previous_close, schedule=schedule)
        assert result == self._dt(10, 0)

    def test_weekend_span_is_the_callers_responsibility_via_previous_session_close(self):
        # This function doesn't know about calendars at all -- it just
        # trusts whatever previous_session_close it's given. Passing the
        # real previous trading session's close (Friday) rather than a
        # naive "yesterday" is what makes Monday-morning runs correctly
        # span the whole weekend -- confirmed here with a Friday close.
        previous_close = datetime(2026, 7, 3, 16, 0, tzinfo=self.ET)  # the preceding Friday
        now = self._dt(9, 21)  # Monday morning
        result = compute_lookback_start(now, previous_close)
        assert result == previous_close
        assert result.weekday() == 4  # Friday
