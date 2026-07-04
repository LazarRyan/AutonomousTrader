"""
Dynamic per-cycle universe discovery.

Real design problem this fixes: run_cycle() used to default to scanning the
full static S&P 500 list (~500 symbols) on every run -- fine for occasional
manual dry runs, but wasteful once the cycle is scheduled 3x/day (see
scripts/launchd/): most of those 500 symbols have nothing new to say most
of the time. Instead, each cycle's universe should be whatever you actually
hold right now, plus whatever tickers just showed up in real news or
congressional PTR filings since the last cycle -- a much smaller, much more
relevant set, discovered fresh each run rather than blindly re-scanning
everything.

Two pure, fully unit-tested pieces live here (no network):

  - rank_discovered_symbols(): given raw ticker-mention lists (one list per
    news article or per parsed filing transaction), counts total mentions
    per ticker, optionally filters to a known valid universe (screens out
    junk/OTC tickers the rest of the pipeline -- insider EDGAR's CIK map in
    particular -- isn't built to handle), and returns the top N by mention
    count with deterministic tie-breaking.

  - compute_lookback_start() / DailySchedule: figures out how far back a
    news/filing discovery pull should look, given the fixed 3x/day
    schedule. The first scheduled slot of a trading day looks back to the
    previous trading SESSION's close (not a fixed hour count) -- this
    deliberately defers to the real market calendar (passed in by the
    caller) so weekends, holidays, and early closes are all handled
    correctly without hardcoding "how many hours is a weekend" anywhere.
    Later slots the same day look back to the immediately preceding slot.

Thin, not-unit-tested network glue that USES these (fetching real news,
fetching real recent filing indexes) lives in signals/news_sentiment.py and
signals/congressional.py respectively, next to each source's other network
wrappers -- same two-tier split as the rest of this project.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time as dtime


def rank_discovered_symbols(
    symbol_mentions: list[list[str]],
    cap: int = 50,
    valid_universe: set[str] | None = None,
) -> list[str]:
    """symbol_mentions is one list of tickers per "thing mentioning
    tickers" (e.g. one News article's .symbols list, or a single-element
    list wrapping one parsed congressional transaction's .ticker) -- counts
    total mentions per ticker across all of them.

    valid_universe, if given, filters OUT any mentioned ticker not in that
    set before ranking -- intended to be the known S&P 500 list, since a
    broad news/filing sweep can surface OTC/foreign/junk tickers the rest
    of this pipeline (insider EDGAR's CIK map in particular) isn't built to
    handle well. Held positions should be passed in separately by the
    caller and unioned in afterwards, NOT filtered through this function --
    a real position you hold should never be dropped from the universe just
    because it fell out of the index.

    Returns the top `cap` tickers by mention count, ties broken
    alphabetically so the result is deterministic given the same input.
    """
    counts: Counter[str] = Counter()
    for mentions in symbol_mentions:
        for raw_symbol in mentions:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            if valid_universe is not None and symbol not in valid_universe:
                continue
            counts[symbol] += 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [symbol for symbol, _count in ranked[:cap]]


@dataclass(frozen=True)
class DailySchedule:
    """The fixed wall-clock times run_cycle() is scheduled at, in the
    exchange's local time zone. Must be given in ascending order -- the
    constructor does not sort them, so a caller passing them out of order
    gets wrong (but deterministic) behavior rather than a silent fix-up.
    """

    slot_times: tuple[dtime, ...] = (dtime(9, 40), dtime(14, 30), dtime(15, 50))


def compute_lookback_start(
    now: datetime,
    previous_session_close: datetime,
    schedule: DailySchedule | None = None,
) -> datetime:
    """How far back this cycle's news/filing discovery pull should look.

    `now` and `previous_session_close` must both be timezone-aware and
    already in the same timezone as `schedule.slot_times` (exchange local
    time) -- this function does no timezone conversion or awareness
    checking itself, that's the caller's responsibility (main.py's caller
    is expected to pass values already normalized via Alpaca's real market
    clock/calendar).

    Behavior:
      - If `now` is at or before the first scheduled slot of the day (or
        earlier -- e.g. a manual/ad-hoc run before 9:40), looks back to
        `previous_session_close`. This is what makes a Monday-morning run
        correctly span the whole weekend, and a post-holiday run span the
        holiday, without hardcoding a fixed hour count anywhere.
      - Otherwise, looks back to the immediately preceding scheduled slot
        the same day (e.g. the 2:30pm run looks back to 9:40am).
    """
    schedule = schedule or DailySchedule()
    slots_today = sorted(schedule.slot_times)

    now_time = now.timetz().replace(tzinfo=None)
    preceding_slots = [t for t in slots_today if t <= now_time]

    if not preceding_slots:
        return previous_session_close

    current_slot_index = len(preceding_slots) - 1
    if current_slot_index == 0:
        return previous_session_close

    previous_slot_time = slots_today[current_slot_index - 1]
    return now.replace(
        hour=previous_slot_time.hour,
        minute=previous_slot_time.minute,
        second=0,
        microsecond=0,
    )
