"""Pre-open check and weekly goal: the pure parts (market day, message text, goal math,
setting validation)."""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import Closure, session_on, set_closures
from smtrading.telegram.preopen import (
    MarketDay,
    PreopenReport,
    clean_preopen,
    market_days,
    render_closed,
    render_report,
)
from smtrading.telegram.weekly_goal import clean_weekly_goal, goal_line

D = Decimal
ROME = ZoneInfo("Europe/Rome")


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


# -- market day ------------------------------------------------------------------------


def test_session_on_regular_holiday_and_manual():
    open_, close, early = session_on("NYSE", date(2027, 3, 15))
    assert (open_, close, early) == (datetime(2027, 3, 15, 13, 30, tzinfo=UTC),
                                     datetime(2027, 3, 15, 20, tzinfo=UTC), False)
    assert session_on("NYSE", date(2027, 3, 13)) is None  # Saturday
    assert session_on("XETRA", date(2026, 12, 24)) is None  # Christmas Eve: closed
    assert session_on("NYSE", date(2026, 11, 27))[2] is True  # day after Thanksgiving
    set_closures([Closure("MTA", date(2027, 3, 15), "closed", None),
                  Closure("NYSE", date(2027, 3, 15), "early_close", time(13))])
    assert session_on("MTA", date(2027, 3, 15)) is None
    _, close, early = session_on("NYSE", date(2027, 3, 15))
    assert close == datetime(2027, 3, 15, 17, tzinfo=UTC) and early is True


def test_market_days_lists_closed_and_early():
    set_closures([Closure("MTA", date(2027, 3, 15), "closed", None),
                  Closure("NYSE", date(2027, 3, 15), "early_close", time(13))])
    days = {d.market: d for d in market_days(date(2027, 3, 15), ROME)}
    assert list(days) == ["NYSE", "NASDAQ", "MTA", "XETRA"]
    assert days["MTA"] == MarketDay("MTA", False, None, True)
    assert days["NYSE"] == MarketDay("NYSE", True, "18:00", True)  # 13:00 ET in Rome
    assert days["NASDAQ"] == MarketDay("NASDAQ", True, None, False)


# -- message text ----------------------------------------------------------------------


def _markets(*closed: str) -> list[MarketDay]:
    return [MarketDay(m, m not in closed, None, False)
            for m in ("NYSE", "NASDAQ", "MTA", "XETRA")]


def test_render_ready():
    text = render_report(PreopenReport(date(2027, 3, 15), _markets(), quotes_checked=3,
                                       problems=[]))
    lines = text.split("\n")
    assert lines[0] == "✅ Pronti per l'apertura — 15/03/2027"
    assert "Mercati aperti oggi: NYSE, NASDAQ, MTA, XETRA" in lines
    assert "Quotazioni: 3 simboli aggiornati" in lines
    assert 3 <= len(lines) - 1 <= 6


def test_render_problems_and_closed_markets():
    markets = _markets("NYSE", "NASDAQ")
    markets[2] = MarketDay("MTA", True, "13:00", True)
    text = render_report(PreopenReport(date(2027, 3, 15), markets, quotes_checked=0,
                                       problems=["Kill switch attivo", "Redis non raggiungibile"]))
    lines = text.split("\n")
    assert lines[0] == "⚠️ Controllo pre-apertura: 2 problemi — 15/03/2027"
    assert lines[1:3] == ["• Kill switch attivo", "• Redis non raggiungibile"]
    assert "Mercati aperti oggi: MTA, XETRA" in lines
    assert "Chiusi oggi: NYSE, NASDAQ" in lines
    assert "Chiusura anticipata: MTA alle 13:00 (chiusura manuale)" in lines
    one = render_report(PreopenReport(date(2027, 3, 15), _markets(), 1, ["Kill switch attivo"]))
    assert one.startswith("⚠️ Controllo pre-apertura: 1 problema — ")


def test_render_closed():
    assert render_closed(date(2027, 12, 25)) == "📅 Mercati chiusi oggi — 25/12/2027"


# -- settings --------------------------------------------------------------------------


def test_clean_preopen():
    assert clean_preopen({"on": False, "at": "09:05"}, {"on": True, "at": "08:45"}) == {
        "on": False, "at": "09:05"}
    assert clean_preopen({"at": "7:30"}, {"on": True, "at": "08:45"}) == {"on": True,
                                                                          "at": "07:30"}
    for bad in ({"at": "25:00"}, {"at": "8"}, {"at": 845}, {"on": "yes"}):
        with pytest.raises(SMTError):
            clean_preopen(bad, {"on": True, "at": "08:45"})


def test_clean_weekly_goal():
    assert clean_weekly_goal({"pct": "2.5"}, {"on": True, "pct": "2.0"}) == {"on": True,
                                                                           "pct": "2.5"}
    assert clean_weekly_goal({"on": False}, {"on": True, "pct": "2.0"}) == {"on": False,
                                                                          "pct": "2.0"}
    for bad in ({"pct": "0"}, {"pct": "-1"}, {"pct": "101"}, {"pct": "1.234"},
                {"pct": "abc"}, {"on": 1}):
        with pytest.raises(SMTError):
            clean_weekly_goal(bad, {"on": True, "pct": "2.0"})


# -- weekly goal -----------------------------------------------------------------------


def test_goal_line_math():
    assert goal_line(D("2.0"), D("1000"), D("1004.2")) == (
        "🎯 Obiettivo settimana +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 € (+0,42%)")
    assert goal_line(D("2.5"), D("2000"), D("1990")) == (
        "🎯 Obiettivo settimana +2,5%: 2.000,00 → 2.050,00 € — attuale 1.990,00 € (-0,50%)")
    assert goal_line(D("10"), D("100"), D("100")) == (
        "🎯 Obiettivo settimana +10%: 100,00 → 110,00 € — attuale 100,00 € (0,00%)")


def test_goal_line_without_data():
    assert goal_line(D("2"), None, None) == (
        "🎯 Obiettivo settimana +2%: dati della settimana non ancora disponibili")
    assert goal_line(D("2"), D("0"), D("0")).endswith("non ancora disponibili")
