import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine import service as service_module
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    cancel_all_orders,
    cancel_order,
    close_all_positions,
    close_position,
    get_order,
    list_orders,
    modify_order,
    place_bracket_order,
    place_oco_order,
    place_order,
    preview_order,
)
from smtrading.engine.triggers import OrderView, TriggerAction, evaluate
from smtrading.events import POST_COMMIT_KEY
from smtrading.ledger.models import AccountKind
from smtrading.ledger.service import get_balance, get_or_create_account
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ and MTA open
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
NASDAQ_CLOSE_TUE = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
NASDAQ_CLOSE_MON = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("ENI.MI", D("14.990"), D("15.000"), D("14.995"))
    q.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    return q


@pytest.fixture
async def p(db_session, seeded):
    return await create_portfolio(
        db_session, name=f"O-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(1),
    )


def _req(p, symbol="AAPL", side=OrderSide.buy, type=OrderType.market, **kw):
    kw.setdefault("qty", D(10))
    return PlaceOrderRequest(portfolio_id=p.id, symbol=symbol, side=side, type=type, **kw)


async def _position(session, portfolio_id, symbol):
    acct = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=symbol, kind=AccountKind.position
    )
    return await get_balance(session, acct.id)


async def _tick(session, qs, order, bid, ask):
    """What the processor does on a tick: evaluate, then execute on `fill`."""
    qs.set_quote(order.symbol, D(bid), D(ask), None)
    quote = (await qs.get_quotes([order.symbol]))[order.symbol]
    await session.refresh(order)
    view = OrderView(
        type=order.type, side=order.side, status=order.status,
        limit_price=order.limit_price, stop_price=order.stop_price,
        trail_pct=order.trail_pct, trail_amount=order.trail_amount,
        trail_ref_price=order.trail_ref_price,
    )
    result = evaluate(view, *effective_bid_ask(QuoteView(quote.bid, quote.ask, None, quote.ts), 2))
    if result.action in (TriggerAction.fill, TriggerAction.trigger_to_market):
        return await execute_fill(session, qs, order.id, quote=quote)
    return None


def _effects(session):
    return list(session.info.get(POST_COMMIT_KEY, []))


async def test_market_order_fills_immediately(db_session, qs, p):
    order = await place_order(db_session, qs, _req(p), now=OPEN)

    assert order.status == OrderStatus.filled
    assert order.filled_qty == D(10) and order.avg_fill_price == D("200.00")
    assert order.expires_at is None  # gtc by default
    fills = (await db_session.scalars(select(Fill).where(Fill.order_id == order.id))).all()
    assert len(fills) == 1
    assert await _position(db_session, p.id, "AAPL") == D(10)
    kinds = [e[:2] for e in _effects(db_session)]
    assert ("event", "order.filled") in kinds
    assert ("track", "AAPL") in kinds  # positions tracking from execute_fill
    assert ("track", "AAPL", "orders") not in _effects(db_session)  # nothing left open


async def test_limit_pending_then_filled_on_tick(db_session, qs, p):
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")), now=OPEN
    )
    assert order.status == OrderStatus.pending
    assert ("track", "AAPL", "orders") in _effects(db_session)
    assert await _position(db_session, p.id, "AAPL") == 0

    assert await _tick(db_session, qs, order, "196.00", "196.10") is None  # not yet
    res = await _tick(db_session, qs, order, "194.90", "195.00")
    assert res is not None and res.order.status == OrderStatus.filled
    assert res.fill.price == D("195.00")
    assert await _position(db_session, p.id, "AAPL") == D(10)


async def test_marketable_limit_fills_at_placement(db_session, qs, p):
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("205.00")), now=OPEN
    )
    assert order.status == OrderStatus.filled
    assert order.avg_fill_price == D("200.00")  # never worse than the limit, at the ask


_SETUP_EVENTS = {"portfolio.created", "portfolio.cash"}  # from the fixtures


async def test_rejection_is_persisted(db_session, qs, p):
    req = _req(p, qty=D(1000), client_order_id="big-1")  # 200k USD = 160k EUR > 100k
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, req, now=OPEN)
    assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER

    rows = (await db_session.scalars(select(Order).where(Order.portfolio_id == p.id))).all()
    assert len(rows) == 1
    rejected = rows[0]
    assert rejected.status == OrderStatus.rejected
    assert rejected.reject_code == "INSUFFICIENT_BUYING_POWER"
    assert rejected.reject_message
    assert rejected.client_order_id == "big-1"
    # only the refusal is published (no tracking, no fill events)
    assert [(e[0], e[1]) for e in _effects(db_session) if e[1] not in _SETUP_EVENTS] == [
        ("event", "order.rejected")]


async def test_execution_failure_rolls_back_savepoint_and_rejects(
    db_session, qs, p, monkeypatch
):
    async def failing_fill(session, qs, order_id, *, quote, **kw):
        await execute_fill(session, qs, order_id, quote=quote)  # fill, journal, effects...
        raise SMTError(ErrorCode.INSUFFICIENT_BUYING_POWER, "boom")

    monkeypatch.setattr(service_module, "execute_fill", failing_fill)
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, _req(p), now=OPEN)
    assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER

    order = await db_session.scalar(select(Order).where(Order.portfolio_id == p.id))
    assert order.status == OrderStatus.rejected and order.reject_code == "INSUFFICIENT_BUYING_POWER"
    assert await db_session.scalar(select(func.count()).select_from(Fill)) == 0
    assert await _position(db_session, p.id, "AAPL") == 0
    # effects queued inside the savepoint are discarded: only the refusal is published
    assert [(e[0], e[1]) for e in _effects(db_session) if e[1] not in _SETUP_EVENTS] == [
        ("event", "order.rejected")]


async def test_idempotent_client_order_id(db_session, qs, p):
    req = _req(p, type=OrderType.limit, limit_price=D("190.00"), client_order_id="c-1")
    first = await place_order(db_session, qs, req, now=OPEN)
    again = await place_order(
        db_session, qs,
        _req(p, type=OrderType.limit, limit_price=D("190.000"), client_order_id="c-1"),
        now=OPEN,
    )
    assert again.id == first.id
    count = await db_session.scalar(
        select(func.count()).select_from(Order).where(Order.portfolio_id == p.id)
    )
    assert count == 1

    with pytest.raises(SMTError) as exc:
        await place_order(
            db_session, qs,
            _req(p, type=OrderType.limit, limit_price=D("191.00"), client_order_id="c-1"),
            now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert "client_order_id reused" in exc.value.message


async def test_market_closed_queues_market_orders(db_session, qs, p):
    order = await place_order(
        db_session, qs, _req(p, tif=TimeInForce.day), now=SATURDAY
    )
    assert order.status == OrderStatus.queued
    assert order.expires_at == NASDAQ_CLOSE_MON
    assert await db_session.scalar(select(func.count()).select_from(Fill)) == 0
    assert ("track", "AAPL", "orders") in _effects(db_session)


async def test_market_closed_queued_even_with_stale_quote(db_session, qs, p):
    qs.set_quote(
        "AAPL", D("199.90"), D("200.00"), D("199.95"),
        ts=datetime.now(UTC) - timedelta(hours=1),  # well past the 120s us_equity limit
    )
    rejected = await preview_order(db_session, qs, _req(p), now=OPEN)
    assert rejected.code == ErrorCode.STALE_QUOTE  # open market: the age matters
    order = await place_order(db_session, qs, _req(p), now=SATURDAY)
    assert order.status == OrderStatus.queued


async def test_market_closed_limit_rests_pending_without_evaluation(db_session, qs, p):
    # marketable limit: would fill if the market were open
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("205.00")), now=SATURDAY
    )
    assert order.status == OrderStatus.pending
    assert await db_session.scalar(select(func.count()).select_from(Fill)) == 0


async def test_day_expiry(db_session, qs, p):
    us = await place_order(
        db_session, qs,
        _req(p, type=OrderType.limit, limit_price=D("190.00"), tif=TimeInForce.day), now=OPEN,
    )
    assert us.expires_at == NASDAQ_CLOSE_TUE
    btc = await place_order(
        db_session, qs,
        _req(p, "BTC-USD", type=OrderType.limit, qty=D("0.1"), limit_price=D("40000.00"),
             tif=TimeInForce.day),
        now=OPEN,
    )
    # next midnight in Europe/Rome (UTC+1 in November)
    assert btc.expires_at == datetime(2026, 11, 10, 23, 0, tzinfo=UTC)


async def test_crypto_open_on_saturday(db_session, qs, p):
    order = await place_order(
        db_session, qs, _req(p, "BTC-USD", qty=D("0.1")), now=SATURDAY
    )
    assert order.status == OrderStatus.filled


async def test_bracket_lifecycle(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    assert entry.status == OrderStatus.pending and entry.role == "entry"
    assert (tp.role, tp.type, tp.side, tp.limit_price) == (
        "take_profit", OrderType.limit, OrderSide.sell, D("210.00")
    )
    assert (sl.role, sl.type, sl.side, sl.stop_price) == (
        "stop_loss", OrderType.stop, OrderSide.sell, D("190.00")
    )
    assert tp.status == sl.status == OrderStatus.held
    assert tp.parent_id == sl.parent_id == entry.id
    assert tp.oco_group_id is not None and tp.oco_group_id == sl.oco_group_id
    assert tp.qty == sl.qty == D(10)

    await _tick(db_session, qs, entry, "194.90", "195.00")  # entry fills
    for o in (entry, tp, sl):
        await db_session.refresh(o)
    assert entry.status == OrderStatus.filled
    assert tp.status == sl.status == OrderStatus.pending

    await _tick(db_session, qs, tp, "210.00", "210.10")  # take profit fills
    for o in (tp, sl):
        await db_session.refresh(o)
    assert tp.status == OrderStatus.filled
    assert sl.status == OrderStatus.canceled
    assert await _position(db_session, p.id, "AAPL") == 0


async def test_bracket_market_entry_releases_children(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D("220.00"), stop_loss=D("180.00"), now=OPEN,
    )
    assert entry.status == OrderStatus.filled
    assert tp.status == sl.status == OrderStatus.pending
    assert ("track", "AAPL", "orders") in _effects(db_session)


@pytest.mark.parametrize(
    ("side", "tp", "sl"),
    [
        (OrderSide.buy, "190.00", "180.00"),  # tp below the reference
        (OrderSide.buy, "220.00", "201.00"),  # sl above the reference (ask 200)
        (OrderSide.sell, "210.00", "190.00"),  # inverted for a sell
    ],
)
async def test_bracket_price_validation(db_session, qs, p, side, tp, sl):
    with pytest.raises(SMTError) as exc:
        await place_bracket_order(
            db_session, qs, _req(p, side=side), take_profit=D(tp), stop_loss=D(sl), now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert await db_session.scalar(
        select(func.count()).select_from(Order).where(Order.portfolio_id == p.id)
    ) == 0


async def test_bracket_requires_market_or_limit_entry(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await place_bracket_order(
            db_session, qs, _req(p, type=OrderType.stop, stop_price=D("210.00")),
            take_profit=D("230.00"), stop_loss=D("190.00"), now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_cancel_bracket_entry_cancels_children(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    canceled = await cancel_order(db_session, entry.id, by="agent")
    assert canceled.status == OrderStatus.canceled
    for o in (tp, sl):
        await db_session.refresh(o)
        assert o.status == OrderStatus.canceled
    effects = _effects(db_session)
    assert ("untrack", "AAPL", "orders") in effects
    assert any(e[:2] == ("event", "order.canceled") and e[2]["by"] == "agent" for e in effects)

    with pytest.raises(SMTError) as exc:
        await cancel_order(db_session, entry.id, by="agent")
    assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_oco_cancel_one_leg_keeps_the_other(db_session, qs, p):
    a, b = await place_oco_order(
        db_session, qs,
        _req(p, type=OrderType.limit, limit_price=D("190.00")),
        _req(p, type=OrderType.stop, stop_price=D("210.00")),
        now=OPEN,
    )
    assert a.status == b.status == OrderStatus.pending
    assert a.oco_group_id is not None and a.oco_group_id == b.oco_group_id

    # Canceling one leg does NOT cancel the other: only a fill does.
    await cancel_order(db_session, a.id, by="agent")
    await db_session.refresh(b)
    assert b.status == OrderStatus.pending
    assert ("untrack", "AAPL", "orders") not in _effects(db_session)  # b still open


async def test_oco_fill_cancels_the_other(db_session, qs, p):
    a, b = await place_oco_order(
        db_session, qs,
        _req(p, type=OrderType.limit, limit_price=D("190.00")),
        _req(p, type=OrderType.stop, stop_price=D("210.00")),
        now=OPEN,
    )
    await _tick(db_session, qs, a, "189.90", "190.00")
    await db_session.refresh(b)
    assert b.status == OrderStatus.canceled


async def test_oco_validation(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await place_oco_order(
            db_session, qs, _req(p, type=OrderType.limit, limit_price=D("190.00")), _req(p),
            now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER
    with pytest.raises(SMTError) as exc:
        await place_oco_order(
            db_session, qs,
            _req(p, type=OrderType.limit, limit_price=D("190.00")),
            _req(p, "ENI.MI", type=OrderType.limit, limit_price=D("14.000")),
            now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_oco_rejection_rejects_both_legs(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await place_oco_order(
            db_session, qs,
            _req(p, type=OrderType.limit, limit_price=D("190.00")),
            _req(p, type=OrderType.stop, qty=D(1000), stop_price=D("210.00")),
            now=OPEN,
        )
    assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    rows = (await db_session.scalars(select(Order).where(Order.portfolio_id == p.id))).all()
    assert len(rows) == 2
    assert {o.status for o in rows} == {OrderStatus.rejected}
    assert {o.reject_code for o in rows} == {"INSUFFICIENT_BUYING_POWER"}


async def test_modify_limit(db_session, qs, p):
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("190.00")), now=OPEN
    )
    modified = await modify_order(
        db_session, qs, order.id, qty=D(20), limit_price=D("191.50"), now=OPEN
    )
    assert modified.id == order.id
    assert (modified.qty, modified.limit_price, modified.status) == (
        D(20), D("191.50"), OrderStatus.pending
    )

    # a parameter that does not belong to the type is refused (no type change)
    with pytest.raises(SMTError) as exc:
        await modify_order(db_session, qs, order.id, stop_price=D("180.00"), now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER

    # risk checks re-run: 1000 shares exceed buying power; the order is unchanged
    with pytest.raises(SMTError) as exc:
        await modify_order(db_session, qs, order.id, qty=D(1000), now=OPEN)
    assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    await db_session.refresh(order)
    assert order.qty == D(20) and order.status == OrderStatus.pending

    await cancel_order(db_session, order.id, by="agent")
    with pytest.raises(SMTError) as exc:
        await modify_order(db_session, qs, order.id, limit_price=D("192.00"), now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_modify_bracket_entry_qty_updates_children(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    await modify_order(db_session, qs, entry.id, qty=D(5), now=OPEN)
    for o in (tp, sl):
        await db_session.refresh(o)
        assert o.qty == D(5)


async def test_close_long_position(db_session, qs, p):
    await place_order(db_session, qs, _req(p, qty=D(10)), now=OPEN)

    partial = await close_position(db_session, qs, p.id, "aapl", D(4), now=OPEN)
    assert (partial.side, partial.type, partial.qty, partial.status) == (
        OrderSide.sell, OrderType.market, D(4), OrderStatus.filled
    )
    assert await _position(db_session, p.id, "AAPL") == D(6)

    with pytest.raises(SMTError) as exc:
        await close_position(db_session, qs, p.id, "AAPL", D(7), now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER

    rest = await close_position(db_session, qs, p.id, "AAPL", source="admin", now=OPEN)
    assert rest.qty == D(6) and rest.source == "admin" and rest.status == OrderStatus.filled
    assert await _position(db_session, p.id, "AAPL") == 0

    with pytest.raises(SMTError) as exc:
        await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_close_short_position(db_session, qs, p):
    await place_order(db_session, qs, _req(p, side=OrderSide.sell, qty=D(5)), now=OPEN)
    assert await _position(db_session, p.id, "AAPL") == D(-5)

    order = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert (order.side, order.qty, order.status) == (OrderSide.buy, D(5), OrderStatus.filled)
    assert await _position(db_session, p.id, "AAPL") == 0


async def test_close_all_positions(db_session, qs, p):
    await place_order(db_session, qs, _req(p, qty=D(10)), now=OPEN)
    await place_order(db_session, qs, _req(p, "ENI.MI", qty=D(100)), now=OPEN)

    orders = await close_all_positions(db_session, qs, p.id, source="system", now=OPEN)
    assert sorted(o.symbol for o in orders) == ["AAPL", "ENI.MI"]
    assert all(o.status == OrderStatus.filled and o.source == "system" for o in orders)
    assert await _position(db_session, p.id, "AAPL") == 0
    assert await _position(db_session, p.id, "ENI.MI") == 0


async def test_notional_only_for_market(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await place_order(
            db_session, qs,
            _req(p, type=OrderType.limit, qty=None, notional=D(1000), limit_price=D("190.00")),
            now=OPEN,
        )
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 0

    order = await place_order(db_session, qs, _req(p, qty=None, notional=D(1000)), now=OPEN)
    assert order.status == OrderStatus.filled and order.notional == D(1000)


@pytest.mark.parametrize(
    "kw",
    [
        {"type": OrderType.trailing_stop},  # neither trail_pct nor trail_amount
        {"type": OrderType.trailing_stop, "trail_pct": D(5), "trail_amount": D(10)},
        {"type": OrderType.trailing_stop, "trail_pct": D(0)},
        {"type": OrderType.trailing_stop, "trail_amount": D(-1)},
        {"type": OrderType.limit},  # missing limit price
        {"type": OrderType.limit, "limit_price": D(0)},
        {"type": OrderType.stop},
        {"type": OrderType.stop_limit, "stop_price": D("210.00")},
        {"type": OrderType.market, "limit_price": D("190.00")},
        {"type": OrderType.limit, "limit_price": D("190.00"), "trail_pct": D(5)},
        {"qty": None},  # neither qty nor notional
        {"qty": D(1), "notional": D(100)},
    ],
)
async def test_invalid_parameters(db_session, qs, p, kw):
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, _req(p, **kw), now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 0


async def test_trailing_stop_reference(db_session, qs, p):
    sell = await place_order(
        db_session, qs,
        _req(p, side=OrderSide.sell, type=OrderType.trailing_stop, trail_pct=D(5)), now=OPEN,
    )
    assert sell.status == OrderStatus.pending and sell.trail_ref_price == D("199.90")
    buy = await place_order(
        db_session, qs,
        _req(p, type=OrderType.trailing_stop, trail_amount=D("5.00")), now=OPEN,
    )
    assert buy.status == OrderStatus.pending and buy.trail_ref_price == D("200.00")


async def test_preview(db_session, qs, p):
    ok = await preview_order(db_session, qs, _req(p))
    assert ok.ok and ok.estimated_price == D("200.00")
    bad = await preview_order(db_session, qs, _req(p, qty=D(1000)))
    assert not bad.ok and bad.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    invalid = await preview_order(db_session, qs, _req(p, type=OrderType.limit))
    assert not invalid.ok and invalid.code == ErrorCode.INVALID_ORDER
    assert await db_session.scalar(select(func.count()).select_from(Order)) == 0


async def test_get_list_and_cancel_all(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await get_order(db_session, uuid.uuid4())
    assert exc.value.code == ErrorCode.NOT_FOUND

    filled = await place_order(db_session, qs, _req(p), now=OPEN)
    a = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("190.00")), now=OPEN
    )
    b = await place_order(
        db_session, qs,
        _req(p, "ENI.MI", type=OrderType.limit, qty=D(100), limit_price=D("14.000")),
        now=OPEN,
    )
    assert (await get_order(db_session, a.id)).id == a.id
    assert {o.id for o in await list_orders(db_session, p.id)} == {filled.id, a.id, b.id}
    pending = await list_orders(db_session, p.id, status=[OrderStatus.pending])
    assert {o.id for o in pending} == {a.id, b.id}
    assert len(await list_orders(db_session, p.id, limit=1)) == 1

    with pytest.raises(SMTError) as exc:
        await cancel_order(db_session, filled.id, by="agent")
    assert exc.value.code == ErrorCode.INVALID_ORDER

    assert await cancel_all_orders(db_session, p.id, symbol="ENI.MI") == 1
    await db_session.refresh(b)
    assert b.status == OrderStatus.canceled
    assert await cancel_all_orders(db_session, p.id) == 1
    await db_session.refresh(a)
    assert a.status == OrderStatus.canceled
    assert await cancel_all_orders(db_session, p.id) == 0


async def test_bracket_entry_rejected_at_fill_cancels_children(db_session, qs, p, monkeypatch):
    """Ruling R17."""

    async def failing_fill(*args, **kwargs):
        raise SMTError(ErrorCode.INSUFFICIENT_BUYING_POWER, "no buying power")

    monkeypatch.setattr(service_module, "execute_fill", failing_fill)
    with pytest.raises(SMTError) as err:
        await place_bracket_order(
            db_session, qs, _req(p), take_profit=D("220.00"), stop_loss=D("180.00"), now=OPEN,
        )
    assert err.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    orders = list(await db_session.scalars(select(Order).where(Order.portfolio_id == p.id)))
    by_role = {o.role: o.status for o in orders}
    assert by_role == {
        "entry": OrderStatus.rejected, "take_profit": OrderStatus.canceled,
        "stop_loss": OrderStatus.canceled,
    }
    canceled = [e for e in _effects(db_session) if e[:2] == ("event", "order.canceled")]
    assert len(canceled) == 2


# -- fix wave: M1, M5, bracket replay, held exits ordering ---------------------------


async def test_oco_sibling_cancel_queues_an_event(db_session, qs, p):
    a, b = await place_oco_order(
        db_session, qs,
        _req(p, type=OrderType.limit, limit_price=D("190.00")),
        _req(p, type=OrderType.stop, stop_price=D("210.00")),
        now=OPEN,
    )
    await _tick(db_session, qs, a, "189.90", "190.00")
    await db_session.refresh(b)
    assert b.status == OrderStatus.canceled
    canceled = [e[2] for e in _effects(db_session) if e[:2] == ("event", "order.canceled")]
    assert [e["order_id"] for e in canceled] == [b.id]


async def test_replayed_rejection_is_raised_again(db_session, qs, p):
    req = _req(p, qty=D(1000), client_order_id="big-2")  # insufficient buying power
    for _ in range(2):
        with pytest.raises(SMTError) as exc:
            await place_order(db_session, qs, req, now=OPEN)
        assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert await db_session.scalar(
        select(func.count()).select_from(Order).where(Order.portfolio_id == p.id)
    ) == 1


async def test_replayed_bracket_rejection_is_raised_again(db_session, qs, p):
    req = _req(p, qty=D(1000), client_order_id="br-big")
    for _ in range(2):
        with pytest.raises(SMTError) as exc:
            await place_bracket_order(
                db_session, qs, req, take_profit=D(250), stop_loss=D(150), now=OPEN
            )
        assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER


async def test_replayed_oco_rejection_is_raised_again(db_session, qs, p):
    legs = (
        _req(p, qty=D(1000), type=OrderType.limit, limit_price=D("190.00"),
             client_order_id="oco-a"),
        _req(p, qty=D(1000), type=OrderType.stop, stop_price=D("210.00"),
             client_order_id="oco-b"),
    )
    for _ in range(2):
        with pytest.raises(SMTError) as exc:
            await place_oco_order(db_session, qs, *legs, now=OPEN)
        assert exc.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER


async def test_bracket_replay_is_looked_up_before_price_validation(db_session, qs, p):
    req = _req(p, client_order_id="br-1")
    first = await place_bracket_order(
        db_session, qs, req, take_profit=D(220), stop_loss=D(180), now=OPEN
    )
    qs.set_quote("AAPL", D("229.90"), D("230.00"), None)  # tp is now below the market
    again = await place_bracket_order(
        db_session, qs, req, take_profit=D(220), stop_loss=D(180), now=OPEN
    )
    assert [o.id for o in again] == [o.id for o in first]


@pytest.mark.parametrize(("role", "change", "ok"), [
    ("take_profit", {"limit_price": D("189.00")}, False),  # below the entry limit
    ("take_profit", {"limit_price": D("230.00")}, True),
    ("stop_loss", {"stop_price": D("196.00")}, False),  # above the entry limit
    ("stop_loss", {"stop_price": D("170.00")}, True),
])
async def test_modify_held_exit_keeps_bracket_ordering(db_session, qs, p, role, change, ok):
    _, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    child = tp if role == "take_profit" else sl
    assert child.status == OrderStatus.held
    if ok:
        modified = await modify_order(db_session, qs, child.id, **change)
        assert getattr(modified, next(iter(change))) == next(iter(change.values()))
    else:
        with pytest.raises(SMTError) as exc:
            await modify_order(db_session, qs, child.id, **change)
        assert exc.value.code == ErrorCode.INVALID_ORDER


async def test_modify_held_stop_cannot_cross_the_sibling(db_session, qs, p):
    """Market entry queued (no reference): the exits are still checked against each other."""
    _, _, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D("210.00"), stop_loss=D("190.00"), now=SATURDAY,
    )
    assert sl.status == OrderStatus.held
    with pytest.raises(SMTError) as exc:
        await modify_order(db_session, qs, sl.id, stop_price=D("215.00"))
    assert exc.value.code == ErrorCode.INVALID_ORDER


# -- residual fix: exit cancel scope, kill switch closes (Ruling R25) -----------------


async def test_close_keeps_exits_of_a_pending_bracket_entry(db_session, qs, p):
    _, tp1, sl1 = await place_bracket_order(  # market entry: exits released
        db_session, qs, _req(p), take_profit=D("220.00"), stop_loss=D("180.00"), now=OPEN,
    )
    entry, tp2, sl2 = await place_bracket_order(  # resting entry: exits held
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )

    await close_position(db_session, qs, p.id, "AAPL", now=OPEN)

    for o in (tp1, sl1, entry, tp2, sl2):
        await db_session.refresh(o)
    assert tp1.status == sl1.status == OrderStatus.canceled
    assert entry.status == OrderStatus.pending
    assert tp2.status == sl2.status == OrderStatus.held


async def test_kill_switch_allows_closing_only(db_session, qs, p):
    from smtrading.risk.settings import set_kill_switch

    await place_order(db_session, qs, _req(p, qty=D(10)), now=OPEN)
    await set_kill_switch(db_session, True)

    for req in (_req(p, qty=D(1)), _req(p, side=OrderSide.sell, qty=D(20))):  # open, flip
        with pytest.raises(SMTError) as exc:
            await place_order(db_session, qs, req, now=OPEN)
        assert exc.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    with pytest.raises(SMTError) as exc:  # nothing held: refused before any quote
        await place_order(db_session, qs, _req(p, "ENI.MI", side=OrderSide.sell), now=OPEN)
    assert exc.value.code == ErrorCode.KILL_SWITCH_ACTIVE

    partial = await place_order(db_session, qs, _req(p, side=OrderSide.sell, qty=D(4)), now=OPEN)
    assert partial.status == OrderStatus.filled
    order = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert (order.qty, order.status) == (D(6), OrderStatus.filled)
    assert await _position(db_session, p.id, "AAPL") == 0


# -- plan 16 Task A: valid_until (good-till-time entries) ---------------------------------

BEFORE_OPEN_TUE = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # NASDAQ opens 14:30 UTC
AFTER_CLOSE_TUE = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)


async def _orders_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Order))


def _limit(p, **kw):
    return _req(p, type=OrderType.limit, limit_price=D("190.00"), **kw)


async def test_valid_until_iso_sets_the_expiry_of_a_gtc_order(db_session, qs, p):
    order = await place_order(
        db_session, qs, _limit(p, valid_until="2026-11-10T19:00:00+01:00"), now=OPEN)
    at = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
    assert order.status == OrderStatus.pending and order.tif == TimeInForce.gtc
    assert order.valid_until == at and order.expires_at == at


async def test_valid_until_accepts_a_datetime_and_a_z_suffix(db_session, qs, p):
    at = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
    a = await place_order(db_session, qs, _limit(p, valid_until=at), now=OPEN)
    b = await place_order(db_session, qs, _limit(p, valid_until="2026-11-10T18:00:00Z"), now=OPEN)
    assert a.valid_until == b.valid_until == at


@pytest.mark.parametrize("now", [OPEN, BEFORE_OPEN_TUE])
async def test_valid_until_close_is_today_s_regular_close(db_session, qs, p, now):
    order = await place_order(db_session, qs, _limit(p, valid_until="close"), now=now)
    assert order.valid_until == NASDAQ_CLOSE_TUE == order.expires_at


async def test_valid_until_close_with_day_tif_keeps_the_earlier_end(db_session, qs, p):
    at = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
    early = await place_order(
        db_session, qs, _limit(p, tif=TimeInForce.day, valid_until=at), now=OPEN)
    assert (early.valid_until, early.expires_at) == (at, at)
    late = await place_order(  # after the session close: the day expiry comes first
        db_session, qs,
        _limit(p, tif=TimeInForce.day, valid_until=datetime(2026, 11, 12, 17, 0, tzinfo=UTC)),
        now=OPEN)
    assert late.expires_at == NASDAQ_CLOSE_TUE


@pytest.mark.parametrize(("symbol", "now", "valid_until"), [
    ("BTC-USD", OPEN, "close"),  # crypto: no session close
    ("AAPL", SATURDAY, "close"),  # no session today
    ("AAPL", AFTER_CLOSE_TUE, "close"),  # today's session already closed
    ("AAPL", OPEN, "2026-11-10T14:00:00Z"),  # in the past
    ("AAPL", OPEN, OPEN),  # at placement
    ("AAPL", OPEN, "2026-11-10T18:00:00"),  # no timezone
    ("AAPL", OPEN, "domani"),
    ("AAPL", OPEN, ""),
    ("AAPL", OPEN, 42),
])
async def test_invalid_valid_until_is_refused_with_its_field(
    db_session, qs, p, symbol, now, valid_until,
):
    qty = D("0.1") if symbol == "BTC-USD" else D(10)
    price = D("40000.00") if symbol == "BTC-USD" else D("190.00")
    req = _req(p, symbol, type=OrderType.limit, qty=qty, limit_price=price,
               valid_until=valid_until)
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, req, now=now)
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert exc.value.details["field"] == "valid_until"
    assert await _orders_count(db_session) == 0
    result = await preview_order(db_session, qs, req, now=now)
    assert not result.ok and result.code == ErrorCode.INVALID_ORDER


async def test_valid_until_iso_is_allowed_on_crypto(db_session, qs, p):
    at = OPEN + timedelta(hours=3)
    order = await place_order(
        db_session, qs,
        _req(p, "BTC-USD", type=OrderType.limit, qty=D("0.1"), limit_price=D("40000.00"),
             valid_until=at.isoformat()),
        now=OPEN)
    assert order.valid_until == order.expires_at == at


async def test_bracket_valid_until_is_for_the_entry_only(db_session, qs, p):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00"),
                             valid_until="close"),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    assert entry.valid_until == entry.expires_at == NASDAQ_CLOSE_TUE
    for child in (tp, sl):  # gtc exits: no end of their own
        assert child.valid_until is None and child.expires_at is None
        assert child.tif == TimeInForce.gtc and child.status == OrderStatus.held


async def test_bracket_valid_until_day_exits_keep_the_day_expiry(db_session, qs, p):
    at = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00"),
                             tif=TimeInForce.day, valid_until=at),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
    )
    assert entry.expires_at == at
    assert tp.expires_at == sl.expires_at == NASDAQ_CLOSE_TUE
    assert tp.valid_until is None and sl.valid_until is None


async def test_bracket_invalid_valid_until_stores_nothing(db_session, qs, p):
    with pytest.raises(SMTError) as exc:
        await place_bracket_order(
            db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00"),
                                 valid_until="close"),
            take_profit=D("210.00"), stop_loss=D("190.00"), now=AFTER_CLOSE_TUE,
        )
    assert exc.value.details["field"] == "valid_until"
    assert await _orders_count(db_session) == 0


async def test_valid_until_replay_and_reuse(db_session, qs, p):
    first = await place_order(
        db_session, qs, _limit(p, valid_until="close", client_order_id="vu-1"), now=OPEN)
    again = await place_order(
        db_session, qs, _limit(p, valid_until="close", client_order_id="vu-1"), now=OPEN)
    assert again.id == first.id
    late = await place_order(  # a replay after the close still returns the order
        db_session, qs, _limit(p, valid_until="close", client_order_id="vu-1"),
        now=AFTER_CLOSE_TUE)
    assert late.id == first.id
    for other in (None, "2026-11-10T20:00:00Z"):
        with pytest.raises(SMTError) as exc:
            await place_order(
                db_session, qs, _limit(p, valid_until=other, client_order_id="vu-1"), now=OPEN)
        assert "client_order_id reused" in exc.value.message
    plain = await place_order(db_session, qs, _limit(p, client_order_id="vu-2"), now=OPEN)
    with pytest.raises(SMTError):
        await place_order(
            db_session, qs, _limit(p, valid_until="close", client_order_id="vu-2"), now=OPEN)
    assert plain.valid_until is None and plain.expires_at is None


# -- closes cancel the standalone exits (a PROTEGGI OCO, a stop alone) ------------------

TEN_ = D(10)


async def _exits(db_session, qs, p, *, qty=TEN_, stop_qty=TEN_, now=OPEN):
    """A PROTEGGI OCO (target 220, stop 180) and a stop placed alone, none reduce-only."""
    oco = await place_oco_order(
        db_session, qs,
        _req(p, side=OrderSide.sell, type=OrderType.limit, qty=qty, limit_price=D(220)),
        _req(p, side=OrderSide.sell, type=OrderType.stop, qty=qty, stop_price=D(180)),
        now=now)
    stop = await place_order(
        db_session, qs,
        _req(p, side=OrderSide.sell, type=OrderType.stop, qty=stop_qty, stop_price=D(170)),
        now=now)
    return [*oco, stop]


async def _exit_activities(db_session, p):
    from smtrading.activity.models import Activity

    await db_session.flush()
    return list(await db_session.scalars(
        select(Activity).where(Activity.portfolio_id == p.id,
                               Activity.type == "order.exits_canceled")))


async def test_full_close_cancels_the_standalone_exits(db_session, qs, p):
    """CHIUDI: once flat, an OCO or a stop that is not reduce-only would open a short."""
    await place_order(db_session, qs, _req(p), now=OPEN)
    exits = await _exits(db_session, qs, p)
    other = await place_order(  # the other side: not an exit, kept
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D(150)), now=OPEN)

    close = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)

    assert close.status == OrderStatus.filled
    for o in (*exits, other):
        await db_session.refresh(o)
    assert {o.status for o in exits} == {OrderStatus.canceled}
    assert other.status == OrderStatus.pending
    [act] = await _exit_activities(db_session, p)
    assert "3 ordini di uscita" in act.summary and "simul" not in act.summary.lower()
    assert await _position(db_session, p.id, "AAPL") == 0


async def test_full_close_with_the_kill_switch_on_cancels_the_exits(db_session, qs, p):
    from smtrading.risk.settings import set_kill_switch

    await place_order(db_session, qs, _req(p), now=OPEN)
    exits = await _exits(db_session, qs, p)
    await set_kill_switch(db_session, True)
    close = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert close.status == OrderStatus.filled
    for o in exits:
        await db_session.refresh(o)
    assert {o.status for o in exits} == {OrderStatus.canceled}


async def test_close_all_positions_cancels_the_standalone_exits(db_session, qs, p):
    await place_order(db_session, qs, _req(p), now=OPEN)
    exits = await _exits(db_session, qs, p)
    await close_all_positions(db_session, qs, p.id, now=OPEN)
    for o in exits:
        await db_session.refresh(o)
    assert {o.status for o in exits} == {OrderStatus.canceled}


async def test_partial_close_cancels_only_the_oversized_exits(db_session, qs, p):
    """Close 6 of 10: the OCO for 10 (both legs) goes, the stop for 4 stays."""
    await place_order(db_session, qs, _req(p), now=OPEN)
    tp, sl, stop = await _exits(db_session, qs, p, stop_qty=D(4))
    await close_position(db_session, qs, p.id, "AAPL", D(6), now=OPEN)
    for o in (tp, sl, stop):
        await db_session.refresh(o)
    assert (tp.status, sl.status, stop.status) == (
        OrderStatus.canceled, OrderStatus.canceled, OrderStatus.pending)
    [act] = await _exit_activities(db_session, p)
    assert act.summary.startswith("Chiusura parziale di AAPL: annullati 2 ordini")
    assert act.data["remaining"] == "4"


async def test_partial_close_queued_cancels_the_oversized_exits_at_once(db_session, qs, p):
    """Market closed: the partial close waits for the open; the oversized OCO is already
    canceled (left alone it would sell 10 of the 4 left)."""
    await place_order(db_session, qs, _req(p), now=OPEN)
    tp, sl, stop = await _exits(db_session, qs, p, qty=D(4), stop_qty=D(10))
    close = await close_position(db_session, qs, p.id, "AAPL", D(6), now=SATURDAY)
    assert close.status == OrderStatus.queued
    for o in (tp, sl, stop):
        await db_session.refresh(o)
    assert (tp.status, sl.status, stop.status) == (
        OrderStatus.pending, OrderStatus.pending, OrderStatus.canceled)


async def test_full_close_cancels_a_closing_side_entry_and_its_held_exits(db_session, qs, p):
    """A resting sell bracket entry on a long: flat, it would open a short."""
    await place_order(db_session, qs, _req(p), now=OPEN)
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p, side=OrderSide.sell, type=OrderType.limit,
                             limit_price=D(230)),
        take_profit=D(200), stop_loss=D(240), now=OPEN)
    await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    for o in (entry, tp, sl):
        await db_session.refresh(o)
    assert {entry.status, tp.status, sl.status} == {OrderStatus.canceled}


# -- plan 25: a suspect quote (quality guard) is never traded on --------------------------

async def test_market_order_on_a_suspect_quote_is_refused_stale(db_session, qs, p):
    qs.set_quote("AAPL", D("229.90"), D("230.00"), D("229.95"), suspect="jump")
    preview = await preview_order(db_session, qs, _req(p), now=OPEN)
    assert preview.code == ErrorCode.STALE_QUOTE
    assert preview.details == {"reason": "suspect_quote"}
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, _req(p), now=OPEN)
    assert exc.value.code == ErrorCode.STALE_QUOTE
    assert exc.value.details["reason"] == "suspect_quote"
    assert await db_session.scalar(select(func.count()).select_from(Fill)) == 0


async def test_marketable_limit_on_a_suspect_quote_rests_pending(db_session, qs, p):
    qs.set_quote("AAPL", D("150.00"), D("150.10"), D("150.05"), suspect="disagree")
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("205.00")), now=OPEN
    )
    assert order.status == OrderStatus.pending
    assert await db_session.scalar(select(func.count()).select_from(Fill)) == 0
