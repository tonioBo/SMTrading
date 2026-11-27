from decimal import Decimal

from smtrading.activity.mapping import (
    MAX_SUMMARY,
    activity_for_event,
    activity_for_notification,
)
from smtrading.activity.models import ActivityLevel


def test_filled_is_executed_and_direct():
    d = activity_for_event("order.filled", {
        "order_id": "o1", "symbol": "BTC-USD", "side": "buy", "qty": Decimal("0.1000"),
        "price": Decimal("50000.00"), "currency": "USD",
    })
    assert d.type == "order.filled"
    assert d.level == ActivityLevel.executed and d.direct is True
    assert d.summary == "Ordine eseguito: buy 0.1 BTC-USD a 50000 USD"
    assert d.data["order_id"] == "o1"


def test_partial_fill_and_accepted_not_direct():
    for t in ("order.accepted", "order.partially_filled", "order.canceled", "order.expired"):
        d = activity_for_event(t, {"symbol": "AAPL"})
        assert d.level == ActivityLevel.info and d.direct is False


def test_rejected_event_is_warning_direct():
    d = activity_for_event("order.rejected", {"symbol": "AAPL"})
    assert d.level == ActivityLevel.warning and d.direct is True
    assert "AAPL" in d.summary


def test_kill_switch_on_critical_off_warning():
    on = activity_for_event("kill_switch", {"on": True, "canceled": 3})
    off = activity_for_event("kill_switch", {"on": False, "canceled": 0})
    assert (on.level, on.direct) == (ActivityLevel.critical, True)
    assert (off.level, off.direct) == (ActivityLevel.warning, True)
    assert on.summary == "Kill switch attivato (3 ordini annullati)"
    assert off.summary == "Kill switch disattivato"


def test_halt_set_and_cleared_are_critical_direct():
    glob = activity_for_event("halt.set", {"scope": "global", "reason": "Drawdown 8%"})
    one = activity_for_event("halt.set", {"scope": "portfolio", "reason": "Volatile"})
    off = activity_for_event("halt.cleared", {"scope": "global", "reason": None})
    off_note = activity_for_event("halt.cleared", {"scope": "portfolio", "reason": "Rientrato"})
    for d in (glob, one, off, off_note):
        assert (d.level, d.direct) == (ActivityLevel.critical, True)
    assert glob.summary == "Aperture sospese su tutti i portafogli: Drawdown 8%"
    assert one.summary == "Aperture sospese: Volatile"
    assert off.summary == "Aperture riattivate su tutti i portafogli"
    assert off_note.summary == "Aperture riattivate: Rientrato"
    assert activity_for_event("halt.set", {}).summary == "Aperture sospese: ?"


def test_cash_and_reset_info():
    dep = activity_for_event("portfolio.cash", {"type": "deposit", "amount": Decimal("1000.00"),
                                                "currency": "EUR"})
    wd = activity_for_event("portfolio.cash", {"type": "withdrawal", "amount": Decimal(5),
                                               "currency": "EUR"})
    assert dep.summary == "Deposito di 1000 EUR" and wd.summary == "Prelievo di 5 EUR"
    assert dep.level == ActivityLevel.info and not dep.direct
    reset = activity_for_event("portfolio.reset", {"new_cash": Decimal(10), "currency": "EUR"})
    assert reset.level == ActivityLevel.info and not reset.direct


def test_liquidation_event_ignored_notification_critical():
    assert activity_for_event("liquidation", {"orders": []}) is None
    d = activity_for_notification("liquidation", {"orders": [{"symbol": "AAPL"}]})
    assert d.type == "liquidation"
    assert (d.level, d.direct) == (ActivityLevel.critical, True)
    for kind in ("daily_loss", "broker.order_unknown"):
        assert activity_for_notification(kind, {}).level == ActivityLevel.critical


def test_warning_notifications_direct():
    for kind in ("margin_warning", "order.rejected", "broker.liquidation_waiting",
                 "broker.fill_price_missing", "order.replace_failed"):
        d = activity_for_notification(kind, {})
        assert (d.level, d.direct) == (ActivityLevel.warning, True), kind


def test_unknown_event_is_none():
    for t in ("notification", "margin.warning", "order.cancel_requested",
              "order.broker_accepted", "portfolio.created", "portfolio.archived",
              "portfolio.unarchived", "something.else"):
        assert activity_for_event(t, {"portfolio_id": "x"}) is None, t


def test_price_alert_notification_info_not_direct():
    d = activity_for_notification("price_alert", {
        "symbol": "AAPL", "condition": "above", "value": "200", "price": "201",
    })
    assert (d.type, d.level, d.direct) == ("price_alert", ActivityLevel.info, False)
    assert "AAPL" in d.summary
    other = activity_for_notification("admin.watch_changed", {})
    assert (other.level, other.direct) == (ActivityLevel.info, False)


def test_summary_truncated_to_500():
    d = activity_for_notification("order.rejected", {"symbol": "AAPL", "message": "x" * 2000})
    assert len(d.summary) == MAX_SUMMARY
    e = activity_for_event("order.rejected", {"symbol": "AAPL", "message": "y" * 2000})
    assert len(e.summary) == MAX_SUMMARY


def test_order_waiting_on_a_suspect_quote_is_a_quiet_warning():
    d = activity_for_event("order.quote_suspect", {"symbol": "NVDA", "reason": "jump"})
    assert d.level == ActivityLevel.warning and d.direct is False
    assert d.summary == ("Ordine NVDA in attesa: prezzo da verificare (salto improvviso),"
                         " si esegue con la prossima quotazione affidabile")
    other = activity_for_event("order.quote_suspect", {"symbol": "NVDA", "reason": "x"})
    assert "(non confermato)" in other.summary


def test_filled_after_partial_fills_reports_cumulative_qty_and_avg_price():
    """Alpaca filled buy 3.583587 INTC in 3 fills (1, 1, 1.583587): the one
    «Ordine eseguito» shows the order's total, not the last fill."""
    d = activity_for_event("order.filled", {
        "symbol": "INTC", "side": "buy", "qty": Decimal("1.583587"),
        "price": Decimal("55.81"), "filled_qty": Decimal("3.583587"),
        "avg_price": Decimal("55.7"), "currency": "USD"})
    assert d.summary == "Ordine eseguito: buy 3.583587 INTC a 55.7 USD"
