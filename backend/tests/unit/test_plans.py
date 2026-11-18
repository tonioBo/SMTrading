"""Piani (plan 17): grace days by horizon, the end of a period, the grace deadline on the
exchange calendars (holidays, manual closures), CRYPTO and FX."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from smtrading.goals.periods import Period
from smtrading.goals.plans import ended_at, grace_days, grace_deadline, last_ended_period
from smtrading.instruments.market_hours import Closure, set_closures

ROME = ZoneInfo("Europe/Rome")


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


@pytest.mark.parametrize(("horizon", "days"), [("week", 1), ("month", 3), ("quarter", 5),
                                               ("year", 10)])
def test_grace_by_horizon(horizon, days):
    assert grace_days(horizon, Period(date(2027, 1, 1), date(2027, 12, 31))) == days


@pytest.mark.parametrize(("length", "days"), [(1, 1), (4, 1), (5, 1), (14, 1), (15, 2),
                                              (30, 3), (64, 6), (65, 7), (100, 10),
                                              (400, 10)])
def test_grace_custom_ten_percent_clamped(length, days):
    start = date(2027, 1, 1)
    end = date.fromordinal(start.toordinal() + length - 1)
    assert grace_days("custom", Period(start, end)) == days


def test_ended_at_is_local_midnight_after_the_last_day():
    assert ended_at(Period(date(2027, 1, 11), date(2027, 1, 17)), ROME) == datetime(
        2027, 1, 17, 23, 0, tzinfo=UTC)
    # summer time
    assert ended_at(Period(date(2027, 6, 1), date(2027, 6, 30)), ROME) == datetime(
        2027, 6, 30, 22, 0, tzinfo=UTC)


def test_last_ended_period():
    # recurring week, Wednesday: last week
    assert last_ended_period("week", date(2027, 1, 4), None, True, date(2027, 1, 13)) == \
        Period(date(2027, 1, 4), date(2027, 1, 10))
    # recurring, first period not over yet
    assert last_ended_period("week", date(2027, 1, 11), None, True,
                             date(2027, 1, 13)) is None
    # not recurring: its only period once over
    assert last_ended_period("month", date(2027, 1, 5), None, False,
                             date(2027, 1, 20)) is None
    assert last_ended_period("month", date(2027, 1, 5), None, False, date(2027, 2, 1)) == \
        Period(date(2027, 1, 1), date(2027, 1, 31))
    assert last_ended_period("custom", date(2027, 1, 5), date(2027, 1, 9), False,
                             date(2027, 1, 10)) == Period(date(2027, 1, 5), date(2027, 1, 9))


def test_deadline_skips_a_holiday():
    # the week ends on Sunday 17 January 2027; Monday 18 is Martin Luther King Day
    end = ended_at(Period(date(2027, 1, 11), date(2027, 1, 17)), ROME)
    assert grace_deadline("NYSE", end, 1) == datetime(2027, 1, 19, 21, 0, tzinfo=UTC)
    assert grace_deadline("MTA", end, 1) == datetime(2027, 1, 18, 16, 30, tzinfo=UTC)
    assert grace_deadline("NYSE", end, 3) == datetime(2027, 1, 21, 21, 0, tzinfo=UTC)


def test_deadline_skips_a_manual_closure():
    end = ended_at(Period(date(2027, 1, 11), date(2027, 1, 17)), ROME)
    set_closures([Closure("NYSE", date(2027, 1, 19), "closed", None)])
    assert grace_deadline("NYSE", end, 1) == datetime(2027, 1, 20, 21, 0, tzinfo=UTC)


def test_deadline_crypto_counts_calendar_days():
    end = ended_at(Period(date(2027, 1, 11), date(2027, 1, 17)), ROME)
    assert grace_deadline("CRYPTO", end, 3) == datetime(2027, 1, 20, 23, 0, tzinfo=UTC)


def test_deadline_fx_weekdays():
    # ends Friday night: Monday's 22:00 UTC is the first
    end = ended_at(Period(date(2027, 1, 1), date(2027, 1, 29)), ROME)
    assert grace_deadline("FX", end, 1) == datetime(2027, 2, 1, 22, 0, tzinfo=UTC)
    assert grace_deadline("FX", end, 5) == datetime(2027, 2, 5, 22, 0, tzinfo=UTC)
