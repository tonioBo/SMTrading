"""When a daily/weekly bar is closed: for an exchange, once its regular session (early and
manual closes included) has closed, plus a short grace for the final print; CRYPTO/FX
(and intraday bars) keep ts + duration <= now. And until when a cached bars list holds."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from smtrading.instruments.market_hours import Closure, set_closures
from smtrading.marketdata.bar_close import bar_is_closed, bars_cache_ttl
from smtrading.marketdata.types import Timeframe

D1, W1, H1 = Timeframe.d1, Timeframe.w1, Timeframe.h1


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


def test_us_daily_bar_closed_after_the_session():
    bar = utc(2026, 11, 17)  # NYSE closes 21:00 UTC (22:00 Rome)
    assert not bar_is_closed("NYSE", D1, bar, utc(2026, 11, 17, 20, 0))  # 21:00 Rome
    assert not bar_is_closed("NYSE", D1, bar, utc(2026, 11, 17, 21, 5))  # grace
    assert bar_is_closed("NYSE", D1, bar, utc(2026, 11, 17, 21, 15))  # 22:15 Rome
    assert bar_is_closed("NASDAQ", D1, utc(2026, 11, 16), utc(2026, 11, 17, 14, 0))


def test_local_midnight_stamps_name_their_session():
    milan = utc(2026, 11, 16, 23)  # 17/11 at 00:00 Milan
    assert not bar_is_closed("MTA", D1, milan, utc(2026, 11, 17, 12))
    assert bar_is_closed("MTA", D1, milan, utc(2026, 11, 17, 16, 50))
    new_york = utc(2026, 11, 17, 5)
    assert not bar_is_closed("NYSE", D1, new_york, utc(2026, 11, 17, 20))
    assert bar_is_closed("NYSE", D1, new_york, utc(2026, 11, 17, 21, 15))


def test_eu_daily_bar_has_a_longer_grace_for_delayed_data():
    bar = utc(2026, 11, 17)  # MTA closes 16:30 UTC (17:30 Rome)
    assert not bar_is_closed("MTA", D1, bar, utc(2026, 11, 17, 16, 45))
    assert bar_is_closed("MTA", D1, bar, utc(2026, 11, 17, 16, 50))


def test_early_and_manual_closes():
    friday = utc(2026, 11, 27)  # the day after Thanksgiving: NYSE closes 18:00 UTC
    assert not bar_is_closed("NYSE", D1, friday, utc(2026, 11, 27, 17, 59))
    assert bar_is_closed("NYSE", D1, friday, utc(2026, 11, 27, 18, 10))
    set_closures([Closure("NYSE", date(2026, 11, 18), "early_close", time(12, 0))])
    # 12:00 New York = 17:00 UTC
    assert bar_is_closed("NYSE", D1, utc(2026, 11, 18), utc(2026, 11, 18, 17, 10))


def test_weekly_bar_closes_with_the_weeks_last_session():
    week = utc(2026, 11, 23)  # Thanksgiving week: last session Friday 27, early close
    assert not bar_is_closed("NYSE", W1, week, utc(2026, 11, 27, 17, 0))
    assert bar_is_closed("NYSE", W1, week, utc(2026, 11, 27, 18, 10))
    assert not bar_is_closed("NYSE", W1, utc(2026, 11, 16), utc(2026, 11, 20, 20, 0))
    assert bar_is_closed("NYSE", W1, utc(2026, 11, 16), utc(2026, 11, 20, 21, 15))


def test_crypto_fx_intraday_and_sessionless_days_keep_the_duration_rule():
    assert not bar_is_closed("CRYPTO", D1, utc(2026, 11, 17), utc(2026, 11, 17, 23, 59))
    assert bar_is_closed("CRYPTO", D1, utc(2026, 11, 17), utc(2026, 11, 18))
    assert not bar_is_closed("FX", D1, utc(2026, 11, 17), utc(2026, 11, 17, 22))
    assert bar_is_closed("NYSE", H1, utc(2026, 11, 17, 15), utc(2026, 11, 17, 16))
    assert not bar_is_closed("NYSE", H1, utc(2026, 11, 17, 15), utc(2026, 11, 17, 15, 59))
    # a bar dated a holiday (no session): ts + 1 day
    assert not bar_is_closed("NYSE", D1, utc(2026, 11, 26), utc(2026, 11, 26, 23))
    assert bar_is_closed("NYSE", D1, utc(2026, 11, 26), utc(2026, 11, 27))
    assert not bar_is_closed(None, D1, utc(2026, 11, 17), utc(2026, 11, 17, 23))


def test_cache_ttl_ends_when_the_next_bar_closes():
    # 21:00 Rome: today's bar closes at 21:10 UTC (close + grace)
    assert bars_cache_ttl("NYSE", D1, utc(2026, 11, 17, 20, 0)) == 70 * 60
    # inside the grace: still today's
    assert bars_cache_ttl("NYSE", D1, utc(2026, 11, 17, 21, 5)) == 5 * 60
    # after it: the next close is tomorrow's, beyond the 6 h cap
    assert bars_cache_ttl("NYSE", D1, utc(2026, 11, 17, 21, 15)) == 21600
    assert bars_cache_ttl("NYSE", W1, utc(2026, 11, 17, 20, 0)) == 70 * 60
    # weekend: next close Monday, capped
    assert bars_cache_ttl("NYSE", D1, utc(2026, 11, 21, 12)) == 21600
    # crypto/fx: the next 00:00 UTC
    assert bars_cache_ttl("CRYPTO", D1, utc(2026, 11, 17, 23, 0)) == 3600
    assert bars_cache_ttl("FX", W1, utc(2026, 11, 17, 23, 30)) == 1800
    # intraday: unchanged
    assert bars_cache_ttl("NYSE", H1, utc(2026, 11, 17, 20, 0)) == 3600
    assert bars_cache_ttl("NYSE", D1, utc(2026, 11, 17, 21, 9, 59, 500000)) == 1


def test_dates_outside_the_calendar_window_keep_the_duration_rule():
    # NYSE's calendar starts 2006-11-16: earlier bars must not raise
    assert bar_is_closed("NYSE", D1, utc(2000, 1, 3), utc(2000, 1, 4))
    assert not bar_is_closed("NYSE", D1, utc(2000, 1, 3), utc(2000, 1, 3, 23))
    assert bar_is_closed("NYSE", W1, utc(2000, 1, 3), utc(2000, 1, 10))
    # a backtest-like range spanning 2005-2007: every bar is classified
    now = utc(2026, 11, 17, 12)
    day, closed = utc(2005, 1, 3), 0
    while day < utc(2007, 12, 31):
        closed += bar_is_closed("NYSE", D1, day, now)
        closed += bar_is_closed("MTA", W1, day, now)
        day += timedelta(days=1)
    assert closed > 0
