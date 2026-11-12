from decimal import Decimal as D

import pytest

from smtrading.engine.models import OrderSide, OrderStatus, OrderType
from smtrading.engine.triggers import OrderView, TriggerAction, TriggerResult, evaluate

B, S = OrderSide.buy, OrderSide.sell


def ov(type_, side, status=OrderStatus.pending, **kw):
    vals = {k: (None if v is None else D(v)) for k, v in kw.items()}
    return OrderView(type=type_, side=side, status=status, **vals)


@pytest.mark.parametrize(
    "side,bid,ask,fires",
    [
        (B, "98", "100", True),
        (B, "98", "100.01", False),
        (B, "98", "99", True),
        (S, "100", "101", True),
        (S, "99.99", "101", False),
        (S, "101", "102", True),
    ],
)
def test_limit(side, bid, ask, fires):
    r = evaluate(ov(OrderType.limit, side, limit_price="100"), D(bid), D(ask))
    assert r.action == (TriggerAction.fill if fires else TriggerAction.none)


@pytest.mark.parametrize(
    "side,bid,ask,fires",
    [
        (B, "98", "100", True),
        (B, "98", "99.99", False),
        (B, "98", "101", True),
        (S, "100", "101", True),
        (S, "100.01", "101", False),
        (S, "99", "100", True),
    ],
)
def test_stop(side, bid, ask, fires):
    r = evaluate(ov(OrderType.stop, side, stop_price="100"), D(bid), D(ask))
    assert r.action == (TriggerAction.trigger_to_market if fires else TriggerAction.none)


def test_stop_limit_pending_triggers_to_limit():
    o = ov(OrderType.stop_limit, S, stop_price="100", limit_price="99")
    assert evaluate(o, D(100), D(101)).action == TriggerAction.trigger_to_limit
    assert evaluate(o, D("100.5"), D(101)).action == TriggerAction.none


def test_stop_limit_triggered_evaluated_as_limit():
    o = ov(
        OrderType.stop_limit, B, status=OrderStatus.triggered, stop_price="100", limit_price="101"
    )
    assert evaluate(o, D(100), D(101)).action == TriggerAction.fill
    assert evaluate(o, D(100), D("101.01")).action == TriggerAction.none


@pytest.mark.parametrize("status", [OrderStatus.queued, OrderStatus.new, OrderStatus.pending])
def test_market_fills(status):
    assert evaluate(ov(OrderType.market, B, status=status), D(1), D(2)).action == TriggerAction.fill


@pytest.mark.parametrize(
    "status", [OrderStatus.filled, OrderStatus.canceled, OrderStatus.held, OrderStatus.expired]
)
def test_inactive_statuses_do_nothing(status):
    assert evaluate(ov(OrderType.market, B, status=status), D(1), D(2)).action == TriggerAction.none
    assert (
        evaluate(ov(OrderType.limit, B, status=status, limit_price="100"), D(1), D(2)).action
        == TriggerAction.none
    )


def test_trailing_sell_pct():
    o = ov(OrderType.trailing_stop, S, trail_pct="5", trail_ref_price="100")
    assert evaluate(o, D(103), D(104)) == TriggerResult(TriggerAction.update_trail, D(103))
    o2 = ov(OrderType.trailing_stop, S, trail_pct="5", trail_ref_price="103")
    r = evaluate(o2, D("97.84"), D(98))
    assert r == TriggerResult(TriggerAction.trigger_to_market, D(103))
    # equality at stop 97.85 triggers
    assert evaluate(o2, D("97.85"), D(98)).action == TriggerAction.trigger_to_market
    assert evaluate(o2, D("97.86"), D(98)) == TriggerResult(TriggerAction.none, None)


def test_trailing_sell_ref_never_decreases():
    o = ov(OrderType.trailing_stop, S, trail_pct="5", trail_ref_price="100")
    assert evaluate(o, D(99), D(100)).action == TriggerAction.none


def test_trailing_buy_amount():
    o = ov(OrderType.trailing_stop, B, trail_amount="2", trail_ref_price="50")
    assert evaluate(o, D(48), D(49)) == TriggerResult(TriggerAction.update_trail, D(49))
    o2 = ov(OrderType.trailing_stop, B, trail_amount="2", trail_ref_price="49")
    assert evaluate(o2, D(50), D("51.0")) == TriggerResult(TriggerAction.trigger_to_market, D(49))
    assert evaluate(o2, D(50), D("50.99")).action == TriggerAction.none


def test_trailing_buy_pct():
    o = ov(OrderType.trailing_stop, B, trail_pct="10", trail_ref_price="50")
    # stop 55
    assert evaluate(o, D(54), D(55)).action == TriggerAction.trigger_to_market
    assert evaluate(o, D(54), D("54.99")).action == TriggerAction.none


def test_trailing_uninitialised_ref_starts_from_quote():
    o = ov(OrderType.trailing_stop, S, trail_pct="5")
    assert evaluate(o, D(100), D(101)) == TriggerResult(TriggerAction.update_trail, D(100))
    ob = ov(OrderType.trailing_stop, B, trail_pct="5")
    assert evaluate(ob, D(100), D(101)) == TriggerResult(TriggerAction.update_trail, D(101))
