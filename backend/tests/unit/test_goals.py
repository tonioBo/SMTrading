"""Goals: the pure parts (periods, business days, elapsed time, status, Telegram lines)."""

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from smtrading.goals.periods import (
    Period,
    business_days,
    calendar_period,
    elapsed_pct,
    goal_period,
    is_business_day,
    previous_period,
)
from smtrading.goals.progress import end_line, progress_line, status_of
from smtrading.instruments.market_hours import set_closures

D = Decimal
ROME = ZoneInfo("Europe/Rome")


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


def test_calendar_periods_and_boundaries():
    assert calendar_period("week", date(2026, 11, 17)) == Period(date(2026, 11, 16),
                                                                  date(2026, 11, 22))
    assert calendar_period("week", date(2026, 11, 22)).start == date(2026, 11, 16)  # Sunday
    assert calendar_period("week", date(2026, 12, 31)) == Period(date(2026, 12, 28),
                                                                  date(2027, 1, 3))
    assert calendar_period("month", date(2027, 2, 14)) == Period(date(2027, 2, 1),
                                                                  date(2027, 2, 28))
    assert calendar_period("month", date(2028, 2, 29)).end == date(2028, 2, 29)
    assert calendar_period("month", date(2026, 12, 5)) == Period(date(2026, 12, 1),
                                                                  date(2026, 12, 31))
    assert calendar_period("quarter", date(2026, 11, 17)) == Period(date(2026, 10, 1),
                                                                     date(2026, 12, 31))
    assert calendar_period("quarter", date(2027, 1, 1)) == Period(date(2027, 1, 1),
                                                                   date(2027, 3, 31))
    assert calendar_period("quarter", date(2027, 6, 30)) == Period(date(2027, 4, 1),
                                                                    date(2027, 6, 30))
    assert calendar_period("year", date(2026, 7, 1)) == Period(date(2026, 1, 1),
                                                                date(2026, 12, 31))
    with pytest.raises(ValueError):
        calendar_period("custom", date(2026, 7, 1))


def test_goal_period_recurring_one_off_and_custom():
    start = date(2026, 11, 18)  # a Wednesday
    # recurring: the period containing today, the first one before start_date
    assert goal_period("week", start, None, True, date(2026, 12, 2)).start == date(2026, 11, 30)
    assert goal_period("week", start, None, True, date(2026, 11, 1)).start == date(2026, 11, 16)
    assert goal_period("month", start, None, True, date(2027, 1, 1)) == Period(
        date(2027, 1, 1), date(2027, 1, 31))
    # one-off: always the period containing start_date
    assert goal_period("quarter", start, None, False, date(2027, 5, 1)) == Period(
        date(2026, 10, 1), date(2026, 12, 31))
    assert goal_period("custom", start, date(2026, 12, 6), False, date(2027, 1, 1)) == Period(
        start, date(2026, 12, 6))
    assert previous_period("week", Period(date(2026, 11, 16), date(2026, 11, 22))) == Period(
        date(2026, 11, 9), date(2026, 11, 15))
    assert previous_period("year", Period(date(2027, 1, 1), date(2027, 12, 31))).start == \
        date(2026, 1, 1)


def test_business_days_skip_weekends_and_holidays():
    assert not is_business_day(date(2026, 11, 21))  # Saturday
    assert is_business_day(date(2026, 11, 26))  # Thanksgiving: the European markets trade
    assert not is_business_day(date(2026, 12, 25))  # Christmas: every main market closed
    # custom period over a weekend and Christmas: 21–28 Dec 2026
    # 21, 22, 23, 24 (NYSE trades, early close), 28: the 25th and the weekend left out
    assert business_days(date(2026, 12, 21), date(2026, 12, 28)) == 5
    assert business_days(date(2026, 12, 2), date(2026, 12, 1)) == 0
    assert is_business_day(date(2090, 1, 2))  # outside the calendars: Monday to Friday


def test_elapsed_pct_counts_business_days():
    week = Period(date(2027, 3, 8), date(2027, 3, 14))
    assert elapsed_pct(week, datetime(2027, 3, 7, 12, tzinfo=ROME)) == 0
    assert elapsed_pct(week, datetime(2027, 3, 8, 0, tzinfo=ROME)) == 0
    assert elapsed_pct(week, datetime(2027, 3, 8, 12, tzinfo=ROME)) == D("10.00")
    assert elapsed_pct(week, datetime(2027, 3, 10, 0, tzinfo=ROME)) == D("40.00")
    assert elapsed_pct(week, datetime(2027, 3, 13, 15, tzinfo=ROME)) == 100  # Saturday
    assert elapsed_pct(week, datetime(2027, 3, 15, 9, tzinfo=ROME)) == 100
    weekend = Period(date(2027, 3, 13), date(2027, 3, 14))  # no business day at all
    assert elapsed_pct(weekend, datetime(2027, 3, 14, 0, tzinfo=ROME)) == D("50.00")


def test_status_rule():
    t = D(2)
    kw = {"started": True, "ended": False}
    assert status_of("min", D("2.1"), t, D(50), **kw) == "raggiunto"
    assert status_of("min", D("2"), t, D(50), **kw) == "raggiunto"
    assert status_of("min", D("0.6"), t, D(50), **kw) == "in linea"  # 1 - 0,5 = 0,5
    assert status_of("min", D("0.4"), t, D(50), **kw) == "in ritardo"
    assert status_of("min", D("-0.2"), t, D(10), **kw) == "in linea"  # 0,2 - 0,5
    assert status_of("min", D("-0.4"), t, D(10), **kw) == "in ritardo"
    assert status_of("min", None, t, D(10), **kw) == "non iniziato"
    assert status_of("min", D(1), t, D(0), started=False, ended=False) == "non iniziato"
    assert status_of("min", D(1), t, D(100), started=True, ended=True) == "concluso"
    assert status_of("min", D(3), t, D(100), started=True, ended=True) == "raggiunto"
    assert status_of("cap", D("2.5"), t, D(50), **kw) == "oltre il tetto"
    assert status_of("cap", D("-3"), t, D(90), **kw) == "in linea"
    assert status_of("cap", D("1"), t, D(100), started=True, ended=True) == "concluso"


def _p(**kw) -> dict:
    base = {"goal_id": uuid4(), "name": "settimanale", "horizon": "week", "scope": "total",
            "portfolio_id": None, "portfolio_name": None, "kind": "min",
            "period_start": date(2027, 3, 8), "period_end": date(2027, 3, 14),
            "start_eur": D("1000.00"), "current_eur": D("1004.20"), "target_eur": D("1020.00"),
            "pct": D("0.42"), "target_pct": D("2.00"), "gap_eur": D("15.80"),
            "elapsed_pct": D(90), "status": "in ritardo", "as_of": None}
    return {**base, **kw}


def test_progress_line():
    today = date(2027, 3, 12)
    assert progress_line(_p(), today) == (
        "🎯 Obiettivo settimanale +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 € "
        "(+0,42%) · in ritardo")
    assert progress_line(_p(name="trimestre", target_pct=D("5.50"), kind="cap",
                            portfolio_name="Prudente", status="in linea"), today) == (
        "🎯 Obiettivo trimestre +5,5% (tetto) · Prudente: 1.000,00 → 1.020,00 € — attuale "
        "1.004,20 € (+0,42%) · in linea")
    assert progress_line(_p(pct=None, start_eur=None), today) == (
        "🎯 Obiettivo settimanale +2%: dati del periodo non ancora disponibili")
    assert progress_line(_p(pct=None, period_start=date(2027, 4, 1)), today) == (
        "🎯 Obiettivo settimanale +2%: dal 01/04/2027")


def test_end_line():
    assert end_line(_p(pct=D("2.10"))) == (
        "🏁 Obiettivo settimanale: raggiunto (+2,10% su +2%)")
    assert end_line(_p(pct=D("-0.5"), portfolio_name="Aggressivo")) == (
        "🏁 Obiettivo settimanale · Aggressivo: non raggiunto (-0,50% su +2%)")
    assert end_line(_p(kind="cap", pct=D("1"))) == (
        "🏁 Obiettivo settimanale: tetto rispettato (+1,00% su +2%)")
    assert end_line(_p(kind="cap", pct=D("3"))).endswith("tetto superato (+3,00% su +2%)")
    assert end_line(_p(pct=None)) == (
        "🏁 Obiettivo settimanale: nessun dato del periodo (obiettivo +2%)")
