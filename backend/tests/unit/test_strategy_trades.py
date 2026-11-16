"""FIFO round trips of the «Strategie» page (performance.strategies.fifo_trades): every
opening order is one trade; exits close the oldest lots of the portfolio+symbol whatever
order they come from (an OCO exit leg has no proposal) and inherit their strategy; the
exit fill's costs are split over the lots it closes by quantity."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smtrading.engine.models import OrderSide
from smtrading.performance.strategies import FillIn, fifo_trades

D = Decimal
T0 = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
P1, P2 = uuid.uuid4(), uuid.uuid4()


def fill(side, qty, price, *, minutes=0, strategy=None, order=None, pid=P1, symbol="AMZN",
         costs=None, ccy="USD"):
    return FillIn(
        order_id=order or uuid.uuid4(), client_order_id=None, portfolio_id=pid,
        symbol=symbol, currency=ccy, side=OrderSide(side), qty=D(qty), price=D(price),
        ts=T0 + timedelta(minutes=minutes), costs=costs or {}, strategy=strategy,
    )


def test_open_trade_only():
    [t] = fifo_trades([fill("buy", "0.51", "258", strategy="STR-103",
                            costs={"EUR": D("0.18")})])
    assert t.strategy == "STR-103" and t.direction == "long"
    assert t.qty == D("0.51") and t.open_qty == D("0.51") and t.closed_at is None
    assert t.entry_price == D(258) and t.exit_price is None and t.realized == 0
    assert t.costs == {"EUR": D("0.18")}


def test_exit_without_strategy_inherits_the_lot_strategy():
    trades = fifo_trades([
        fill("buy", 2, 100, strategy="STR-1", costs={"USD": D(1)}),
        fill("sell", 2, 110, minutes=5, costs={"USD": D(1)}),  # OCO leg, no proposal
    ])
    [t] = trades
    assert t.strategy == "STR-1" and t.open_qty == 0
    assert t.realized == D(20) and t.exit_price == D(110)
    assert t.closed_at == T0 + timedelta(minutes=5)
    assert t.costs == {"USD": D(2)}


def test_fifo_partial_closes_split_costs_by_quantity():
    a, b = fill("buy", 1, 100, strategy="A"), fill("buy", 3, 120, minutes=1, strategy="B")
    exit_ = fill("sell", 2, 130, minutes=2, costs={"EUR": D("0.40")})
    ta, tb = fifo_trades([a, b, exit_])
    assert (ta.strategy, ta.open_qty, ta.realized) == ("A", 0, D(30))
    assert ta.costs == {"EUR": D("0.20")}
    assert (tb.strategy, tb.open_qty, tb.realized) == ("B", D(2), D(10))
    assert tb.costs == {"EUR": D("0.20")} and tb.closed_at is None
    assert tb.exit_price == D(130)


def test_several_fills_of_one_order_are_one_trade():
    oid = uuid.uuid4()
    [t] = fifo_trades([
        fill("buy", 1, 100, order=oid, strategy="S"),
        fill("buy", 1, 110, minutes=1, order=oid, strategy="S"),
        fill("sell", 1, 120, minutes=2), fill("sell", 1, 90, minutes=3),
    ])
    assert t.qty == D(2) and t.entry_price == D(105)
    assert t.realized == D(20) + D(-20) and t.exit_price == D(105)
    assert t.closed_at == T0 + timedelta(minutes=3)


def test_short_and_flip():
    trades = fifo_trades([
        fill("sell", 1, 100, strategy="S"),        # opens a short
        fill("buy", 3, 90, minutes=1, strategy="L", costs={"USD": D(3)}),  # closes, flips
    ])
    short, long_ = trades
    assert short.direction == "short" and short.realized == D(10) and short.open_qty == 0
    assert short.costs == {"USD": D(1)}
    assert long_.direction == "long" and long_.strategy == "L"
    assert long_.qty == D(2) and long_.open_qty == D(2) and long_.costs == {"USD": D(2)}


def test_portfolios_and_symbols_are_separate_and_sorted_by_time():
    trades = fifo_trades([
        fill("sell", 1, 110, minutes=5, pid=P2),  # nothing to close in P2: opens a short
        fill("buy", 1, 100, pid=P1, strategy="S"),
        fill("buy", 1, 50, minutes=1, pid=P1, symbol="SPY", strategy="S"),
    ])
    assert [(t.portfolio_id, t.symbol, t.direction) for t in trades] == [
        (P1, "AMZN", "long"), (P1, "SPY", "long"), (P2, "AMZN", "short")]
    assert all(t.open_qty for t in trades)


def test_unrealized_uses_the_open_lots_not_the_trade_average():
    order = uuid.uuid4()
    [t] = fifo_trades([
        fill("buy", 1, 100, order=order),
        fill("buy", 1, 120, minutes=1, order=order),  # same order, second fill
        fill("sell", 1, 125, minutes=2),  # closes the 100 lot (FIFO)
    ])
    assert t.open_qty == 1 and t.open_value == D(120) and t.realized == D(25)
    assert t.unrealized(D(130)) == D(10)  # not (130 - 110) × 1
    [s] = fifo_trades([fill("sell", 2, 50), fill("buy", 1, 40, minutes=1)])  # short
    assert s.unrealized(D(45)) == D(5)
