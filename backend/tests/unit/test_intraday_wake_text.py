"""Wake title and text: built only from the trigger and portfolio data (no fixed names)."""

from datetime import UTC, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from smtrading.intraday.wake import wake_text, wake_title
from smtrading.intraday.watch import Trigger
from smtrading.portfolios.models import Portfolio

ROME = ZoneInfo("Europe/Rome")


def _trigger(**kw) -> Trigger:
    base = {"portfolio_id": None, "ref": "rsi-1", "name": "Strat Prova Q", "symbol": "QQQ9",
            "rule": "entry", "timeframe": "15m",
            "bar_close": datetime(2026, 12, 1, 14, 45, tzinfo=UTC),
            "value": D("28.5"), "threshold": D(30), "price": D("123.4"),
            "rule_text": "rsi sotto 30", "late": False}
    base.update(kw)
    return Trigger(**base)


def test_title_uses_code_then_name():
    assert wake_title(_trigger(), Portfolio(code="ZZT", name="Fittizio Uno")) == \
        "SEGNALE · ZZT · QQQ9 · entrata"
    assert wake_title(_trigger(rule="exit"), Portfolio(code=None, name="Fittizio Uno")) == \
        "SEGNALE · Fittizio Uno · QQQ9 · uscita"


def test_text_has_values_and_bar_time():
    text = wake_text(_trigger(), Portfolio(code="ZZT", name="x"), ROME)
    assert "28,5" in text and "30" in text and "123,4" in text
    assert "15:45" in text  # 14:45Z in Europe/Rome (CET)
    assert "rsi sotto 30" in text and "15m" in text and "rsi-1" in text
    assert text.splitlines()[-1] == "Decidi tu se e come operare: SMTrading non ha inviato ordini."


def test_text_built_from_portfolio_and_agent_data():
    p1 = Portfolio(code="ZZT", name="Fittizio Uno")
    t1 = _trigger()
    assert "ZZT" in wake_title(t1, p1) and "Strat Prova Q" in wake_text(t1, p1, ROME)
    p2 = Portfolio(code=None, name="Fittizio Uno")
    assert "Fittizio Uno" in wake_title(t1, p2) and "ZZT" not in wake_title(t1, p2)
    # other fake data: title and text change, the template around them does not
    t2 = _trigger(name="Altra Zeta", ref="ref-9", symbol="WWW1", rule_text="cond zz")
    p3 = Portfolio(code="KKK", name="Altro")
    assert wake_title(t2, p3) != wake_title(t1, p1)
    a = wake_text(t1, p1, ROME).replace("Strat Prova Q", "{n}").replace("rsi-1", "{r}") \
        .replace("rsi sotto 30", "{x}").replace("QQQ9", "{s}")
    b = wake_text(t2, p3, ROME).replace("Altra Zeta", "{n}").replace("ref-9", "{r}") \
        .replace("cond zz", "{x}").replace("WWW1", "{s}")
    assert a == b
