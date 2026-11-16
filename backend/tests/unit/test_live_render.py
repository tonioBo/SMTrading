"""Telegram text of the real-money activities (pure)."""

import uuid
from datetime import UTC, datetime

import pytest

from smtrading.activity.models import Activity
from smtrading.brokers.live.protect import it_number, render_live_activity
from smtrading.telegram.render import Names

TZ = "Europe/Rome"
TS = datetime(2027, 3, 10, 14, 5, tzinfo=UTC)  # 15:05 Rome
PID, AID = uuid.uuid4(), uuid.uuid4()
NAMES = Names(agents={AID: "Trader"}, portfolios={PID: "Reale 1"})


def _order(phase: str, **data) -> Activity:
    base = {"symbol": "AAPL", "side": "buy", "qty": "0.5", "type": "market",
            "currency": "USD", "source": "agent"}
    return Activity(type=f"live.order.{phase}", level="info", portfolio_id=PID, agent_id=AID,
                    summary="x", data={**base, **data}, ts=TS)


@pytest.mark.parametrize(("phase", "first"), [
    ("sent", "🔴 REALE · inviato"),
    ("filled", "🔴 REALE · eseguito"),
    ("partial", "🔴 REALE · parziale"),
    ("rejected", "🔴 REALE · rifiutato"),
    ("canceled", "🔴 REALE · annullato"),
    ("dry_run", "🧪 REALE (prova)"),
])
def test_render_first_lines_per_phase(phase, first):
    lines = render_live_activity(_order(phase), NAMES, TZ).split("\n")
    assert lines[0] == first
    assert lines[1] == "Reale 1"
    assert lines[2] == "buy 0,5 AAPL · mercato"
    assert lines[-1] == "Agente: Trader · 15:05"


def test_render_sent_exact():
    assert render_live_activity(_order("sent"), NAMES, TZ) == (
        "🔴 REALE · inviato\nReale 1\nbuy 0,5 AAPL · mercato\nAgente: Trader · 15:05")


def test_render_italian_decimals():
    a = _order("filled", qty="1.50", price="201.2500", value="301.875", fees="0.02")
    text = render_live_activity(a, NAMES, TZ)
    assert "buy 1,5 AAPL · mercato" in text
    assert "Prezzo 201,25 USD · valore 301,875 USD · commissioni 0,02" in text
    assert it_number("1234.50") == "1234,5"
    assert it_number(None) == ""
    assert it_number("abc") == ""


def test_render_partial_shows_filled_qty():
    a = _order("partial", qty="2", filled_qty="0.5", price="10", value="5")
    assert "Eseguito 0,5 · Prezzo 10 USD · valore 5 USD" in render_live_activity(a, NAMES, TZ)


def test_render_limit_stop_notional_and_origin():
    a = _order("sent", type="stop_limit", stop_price="140", limit_price="139.5")
    assert "buy 0,5 AAPL · stop 140 limite 139,5" in render_live_activity(a, NAMES, TZ)
    a = _order("sent", type="limit", limit_price="150.10", qty=None, notional="100")
    a.agent_id = None
    a.data["source"] = "admin"
    text = render_live_activity(a, NAMES, TZ)
    assert "buy AAPL per 100 USD · limite 150,1" in text
    assert text.endswith("Origine: admin · 15:05")


def test_render_rejected_with_broker_message_cleaned():
    a = _order("rejected", message="insufficient\x07 buying power")
    text = render_live_activity(a, NAMES, TZ)
    assert "Messaggio: insufficient buying power" in text
    assert "\x07" not in text
    assert "Messaggio" not in render_live_activity(_order("filled", message="ok"), NAMES, TZ)


def test_render_dry_run_says_not_sent():
    assert "Ordine non inviato (prova a secco)" in render_live_activity(
        _order("dry_run"), NAMES, TZ)


def test_render_alert():
    a = Activity(type="live.reduce_only", level="critical", portfolio_id=PID,
                 summary="Portafoglio in reduce-only: conto cambiato", data={"cause": "x"},
                 ts=TS)
    assert render_live_activity(a, NAMES, TZ) == (
        "🔴 REALE · reduce-only automatico\nReale 1\n"
        "Portafoglio in reduce-only: conto cambiato\n15:05")
    a.type = "live.something_new"
    assert render_live_activity(a, NAMES, TZ).startswith("🔴 REALE · something_new")


def test_render_fill_details_before_agent_and_time():
    extra = ["Strategia: STR-103", "Stop 190,00 USD · obiettivo 210,00 USD"]
    lines = render_live_activity(_order("filled", price="200"), NAMES, TZ, extra).split("\n")
    assert lines[-3:] == [*extra, "Agente: Trader · 15:05"]
