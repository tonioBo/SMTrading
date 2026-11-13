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
    assert _effects(db_session) == []


async def test_execution_failure_rolls_back_savepoint_and_rejects(
    db_session, qs, p, monkeypatch
):
    async def failing_fill(session, qs, order_id, *, quote):
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
    assert _effects(db_session) == []  # effects queued inside the savepoint are discarded


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
