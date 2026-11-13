"""Ruling R22 / I1: reduce-only closes and bracket exits; Ruling R16: closes cancel exits."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from smtrading.engine.execution import NOTHING_TO_REDUCE, execute_fill
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_all_positions,
    close_position,
    place_bracket_order,
    place_order,
)
from smtrading.events import POST_COMMIT_KEY
from smtrading.ledger.models import AccountKind, Journal
from smtrading.ledger.service import get_balance, get_or_create_account
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    return q


@pytest.fixture
async def p(db_session, seeded):
    return await create_portfolio(
        db_session, name=f"R-{uuid.uuid4().hex[:8]}", initial_cash=D(100000),
    )


def _req(p, side=OrderSide.buy, qty=10, **kw):
    return PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=side, type=OrderType.market, qty=D(qty), **kw
    )


async def _position(session, portfolio_id, symbol="AAPL"):
    acct = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=symbol, kind=AccountKind.position
    )
    return await get_balance(session, acct.id)


async def _fill(session, qs, order):
    quote = (await qs.get_quotes([order.symbol]))[order.symbol]
    return await execute_fill(session, qs, order.id, quote=quote)


async def _count(session, model, order_id):
    column = Fill.order_id if model is Fill else Journal.order_id
    return await session.scalar(select(func.count()).select_from(model).where(column == order_id))


def _canceled_events(session):
    return [
        e[2] for e in session.info.get(POST_COMMIT_KEY, [])
        if e[0] == "event" and e[1] == "order.canceled"
    ]


async def test_double_close_while_market_closed(db_session, qs, p):
    await place_order(db_session, qs, _req(p), now=OPEN)
    first = await close_position(db_session, qs, p.id, "AAPL", now=SATURDAY)
    second = await close_position(db_session, qs, p.id, "AAPL", now=SATURDAY)
    assert first.status == second.status == OrderStatus.queued
    assert first.reduce_only and second.reduce_only

    # the market opens: the processor executes both queued closes
    assert (await _fill(db_session, qs, first)).fill.qty == D(10)
    assert await _fill(db_session, qs, second) is None
    await db_session.refresh(second)
    assert second.status == OrderStatus.canceled
    assert second.reject_message == NOTHING_TO_REDUCE
    assert await _position(db_session, p.id) == 0  # no reverse (short) position
    assert await _count(db_session, Fill, second.id) == 0
    assert await _count(db_session, Journal, second.id) == 0
    assert any(
        e["order_id"] == second.id and e["reason"] == NOTHING_TO_REDUCE
        for e in _canceled_events(db_session)
    )


async def test_reduce_only_is_clamped_to_the_position(db_session, qs, p):
    await place_order(db_session, qs, _req(p), now=OPEN)
    close = await close_position(db_session, qs, p.id, "AAPL", now=SATURDAY)  # qty 10
    db_session.add(Order(  # meanwhile 4 are sold by another order
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.sell, type=OrderType.market,
        qty=D(4), status=OrderStatus.queued,
    ))
    await db_session.flush()
    other = await db_session.scalar(
        select(Order).where(Order.portfolio_id == p.id, Order.qty == D(4))
    )
    await _fill(db_session, qs, other)

    result = await _fill(db_session, qs, close)
    assert result.fill.qty == D(6)
    assert result.order.filled_qty == D(6) and result.order.status == OrderStatus.filled
    assert await _position(db_session, p.id) == 0


async def test_reduce_only_on_the_same_side_is_canceled(db_session, qs, p):
    await place_order(db_session, qs, _req(p), now=OPEN)
    order = Order(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
        qty=D(5), status=OrderStatus.queued, reduce_only=True,
    )
    db_session.add(order)
    await db_session.flush()
    assert await _fill(db_session, qs, order) is None
    assert order.status == OrderStatus.canceled
    assert await _position(db_session, p.id) == D(10)


async def test_bracket_exits_are_reduce_only(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D(250), stop_loss=D(150), now=OPEN
    )
    assert entry.status == OrderStatus.filled and not entry.reduce_only
    assert tp.reduce_only and sl.reduce_only


async def test_close_position_cancels_bracket_exits(db_session, qs, p):
    _, tp, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D(250), stop_loss=D(150), now=OPEN
    )
    assert tp.status == sl.status == OrderStatus.pending
    close = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert close.status == OrderStatus.filled
    await db_session.refresh(tp)
    await db_session.refresh(sl)
    assert tp.status == sl.status == OrderStatus.canceled
    assert {e["order_id"] for e in _canceled_events(db_session)} >= {tp.id, sl.id}


async def test_close_all_positions_cancels_bracket_exits(db_session, qs, p):
    _, tp, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D(250), stop_loss=D(150), now=OPEN
    )
    [close] = await close_all_positions(db_session, qs, p.id, now=OPEN)
    assert close.status == OrderStatus.filled and close.reduce_only
    await db_session.refresh(tp)
    await db_session.refresh(sl)
    assert tp.status == sl.status == OrderStatus.canceled


async def test_stop_loss_after_manual_sell_does_not_open_a_short(db_session, qs, p):
    _, _, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D(250), stop_loss=D(150), now=OPEN
    )
    # a plain sell (not close_position) leaves the exits open
    await place_order(db_session, qs, _req(p, side=OrderSide.sell), now=OPEN)
    assert await _position(db_session, p.id) == 0
    await db_session.refresh(sl)
    assert sl.status == OrderStatus.pending

    qs.set_quote("AAPL", D("139.90"), D("140.00"), None)  # the stop fires
    sl.status = OrderStatus.triggered
    await db_session.flush()
    assert await _fill(db_session, qs, sl) is None
    assert sl.status == OrderStatus.canceled and sl.reject_message == NOTHING_TO_REDUCE
    assert await _position(db_session, p.id) == 0
