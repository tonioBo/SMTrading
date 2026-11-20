"""The weekly PDF report's caption and setting (pure)."""

from datetime import date, time
from decimal import Decimal

import pytest

from smtrading.common.errors import SMTError
from smtrading.telegram.outbox import CAPTION_MAX_LEN
from smtrading.telegram.render import utf16_len
from smtrading.telegram.weekly_report import caption, filename
from smtrading.telegram.weekly_report_settings import (
    WeeklyReportSettings,
    _stored,
    clean_weekly_report,
    weekly_report_dto,
)

D = Decimal


def _data(**kw):
    data = {"from": date(2027, 3, 8), "to": date(2027, 3, 12),
            "total": {"change_eur": D("-12.5"), "return_pct": D("-0.4167")},
            "agent_costs": {"cost_usd": D("7.25")},
            "weekly_goal": {"target_pct": D("2"), "pct": D("-0.42"), "reached": False},
            "outside_totals": None}
    data.update(kw)
    return data


def test_caption_not_reached_and_unknown_values():
    assert caption(_data()).split("\n") == [
        "📄 Resoconto settimanale — 08/03–12/03/2027",
        "P&L totale: −12,50 € (−0,42%)",
        "Obiettivo settimanale +2,0%: non raggiunto (ora −0,42%)",
        "Costo agenti: 7,25 USD",
    ]
    text = caption(_data(total={"change_eur": None, "return_pct": None},
                         outside_totals={"portfolios": [{"name": "X"}, {"name": "Y"}]}))
    assert "P&L totale: n.d. (n.d.)" in text
    assert text.endswith("Fuori dai totali: 2 portafogli (pagina a parte)")
    assert utf16_len(text) < CAPTION_MAX_LEN


def test_filename():
    assert filename(date(2027, 3, 8), date(2027, 3, 12)) == (
        "resoconto-settimanale-2027-03-08-2027-03-12.pdf")


def test_settings_defaults_and_validation():
    assert _stored(None) == WeeklyReportSettings(True, 4, time(22, 30))
    assert _stored({"on": False, "weekday": 9, "at": "x"}) == WeeklyReportSettings(
        False, 4, time(22, 30))  # a broken stored value falls back to the defaults
    current = weekly_report_dto(None)
    assert current == {"on": True, "weekday": 4, "at": "22:30"}
    assert clean_weekly_report({"weekday": 0, "at": "7:05"}, current) == {
        "on": True, "weekday": 0, "at": "07:05"}
    for bad in ({"weekday": 7}, {"weekday": True}, {"at": "24:00"}, {"on": 1}):
        with pytest.raises(SMTError):
            clean_weekly_report(bad, current)
