from datetime import UTC, date, datetime, time

import pytest

from smtrading.instruments.market_hours import EARLY_CLOSE, Closure, set_closures
from smtrading.intraday.calendar import (
    bar_closes,
    day_close_at,
    due_bar_close,
    last_sessions,
    session_after,
    session_window,
)
from smtrading.marketdata import bar_close
from smtrading.marketdata.bar_close import TIMEFRAME_DURATION
from smtrading.marketdata.types import Timeframe

M5, M15, H1 = Timeframe.m5, Timeframe.m15, Timeframe.h1


def z(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def test_thanksgiving_has_no_bar_closes():
    assert bar_closes("NYSE", M15, date(2026, 11, 26)) == []
    assert due_bar_close("NYSE", M15, z(2026, 11, 26, 16)) is None


def test_half_day_last_close_is_13_et():
    c = bar_closes("NYSE", M15, date(2026, 11, 27))
    assert c[0] == z(2026, 11, 27, 14, 45)
    assert c[-1] == z(2026, 11, 27, 18)
    assert len(c) == 14
    h = bar_closes("NYSE", H1, date(2026, 11, 27))
    assert h[0] == z(2026, 11, 27, 15)
    assert h[-1] == z(2026, 11, 27, 18)


def test_regular_day_counts():
    d = date(2026, 12, 1)
    m15 = bar_closes("NYSE", M15, d)
    assert len(m15) == 26 and m15[0] == z(2026, 12, 1, 14, 45) and m15[-1] == z(2026, 12, 1, 21)
    h1 = bar_closes("NYSE", H1, d)
    assert len(h1) == 7 and h1[0] == z(2026, 12, 1, 15) and h1[-1] == z(2026, 12, 1, 21)
    assert len(bar_closes("NYSE", M5, d)) == 78


def test_dst_week():
    assert bar_closes("NYSE", H1, date(2026, 3, 9))[0] == z(2026, 3, 9, 14)


def test_due_bar_close_waits_for_delay():
    assert due_bar_close("NYSE", M15, z(2026, 12, 1, 15, 0, 59)) == z(2026, 12, 1, 14, 45)
    assert due_bar_close("NYSE", M15, z(2026, 12, 1, 15, 1, 0)) == z(2026, 12, 1, 15)
    assert due_bar_close("NYSE", M15, z(2026, 12, 1, 21, 0, 30)) is None
    assert due_bar_close("NYSE", M15, z(2026, 12, 1, 14, 40)) is None  # before the first close


def test_manual_early_close():
    set_closures([Closure("NYSE", date(2026, 12, 2), EARLY_CLOSE, time(12, 0))])
    try:
        assert bar_closes("NYSE", M15, date(2026, 12, 2))[-1] == z(2026, 12, 2, 17)
        assert day_close_at("NYSE", z(2026, 12, 2, 15), 10) == z(2026, 12, 2, 16, 50)
    finally:
        set_closures([])


def test_last_sessions_skips_holiday():
    assert last_sessions("NYSE", z(2026, 11, 30, 15)) == [
        date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25),
        date(2026, 11, 27), date(2026, 11, 30),
    ]
    assert session_after("NYSE", date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert session_after("NYSE", date(2026, 11, 25), 2) == date(2026, 11, 30)


def test_crypto_has_no_window():
    now = z(2026, 12, 1, 15)
    assert session_window("CRYPTO", now) is None
    assert bar_closes("CRYPTO", M15, date(2026, 12, 1)) == []
    assert due_bar_close("FX", M15, now) is None
    assert last_sessions("CRYPTO", now) == []


def test_session_window_regular_day():
    assert session_window("NYSE", z(2026, 12, 1, 15)) == (z(2026, 12, 1, 14, 30), z(2026, 12, 1, 21))
    assert session_window("NYSE", z(2026, 11, 26, 15)) is None


def test_session_after_without_session_raises():
    with pytest.raises(ValueError):
        session_after("CRYPTO", date(2026, 12, 1), 1)


def test_duration_alias_is_the_same_map():
    assert TIMEFRAME_DURATION is bar_close._DURATION
