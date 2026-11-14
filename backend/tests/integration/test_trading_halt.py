"""«Blocco aperture» (trading halt): the service, the pre-trade check and the fill-time check.

While a halt covers a portfolio (global or on that portfolio) an order that would increase
exposure is refused with TRADING_HALTED; reducing orders, bracket exits, closes and
liquidations go through (the kill switch's exposure test)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smtrading.activity.models import Activity
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.processor import REJECTING_CODES
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    place_bracket_order,
    place_oco_order,
    place_order,
)
from smtrading.engine.triggers import OrderView, TriggerAction, evaluate
from smtrading.ledger.models import AccountKind
from smtrading.ledger.service import get_balance, get_or_create_account
from smtrading.portfolios.service import create_portfolio
from smtrading.risk import halts
from smtrading.risk.models import TradingHalt
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
ADMIN = "admin:admin@example.com"


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    return q


async def _p(session):
    return await create_portfolio(
        session, name=f"H-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(1),
    )


@pytest.fixture
async def p(db_session, seeded):
    return await _p(db_session)


def _req(p, symbol="AAPL", side=OrderSide.buy, type=OrderType.market, **kw):
    kw.setdefault("qty", D(10))
    return PlaceOrderRequest(portfolio_id=p.id, symbol=symbol, side=side, type=type, **kw)


async def _held(session, portfolio_id, symbol="AAPL"):
    acct = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=symbol, kind=AccountKind.position
    )
    return await get_balance(session, acct.id)


async def _tick(session, qs, order, bid, ask):
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


async def _halt(session, portfolio=None, reason="Drawdown oltre soglia"):
    halt, changed = await halts.set_halt(
        session, scope="portfolio" if portfolio else "global",
        portfolio_id=portfolio.id if portfolio else None, reason=reason, by=ADMIN,
    )
    assert changed
    return halt


async def _refused(session, qs, req):
    with pytest.raises(SMTError) as exc:
        await place_order(session, qs, req, now=OPEN)
    assert exc.value.code == ErrorCode.TRADING_HALTED
    assert exc.value.persisted  # the rejection is stored, like KILL_SWITCH_ACTIVE
    return exc.value


# --- service ----------------------------------------------------------------------------


async def test_set_clear_keeps_history_and_records_critical_activity(db_session, p):
    halt = await _halt(db_session, reason="  Mercato\x07 instabile  ")
    assert (halt.scope, halt.portfolio_id, halt.active) == ("global", None, True)
    assert halt.reason == "Mercato instabile" and halt.set_by == ADMIN
    assert (await halts.covering_halt(db_session, p.id)).id == halt.id

    same, changed = await halts.set_halt(
        db_session, scope="global", portfolio_id=None, reason="Mercato instabile", by=ADMIN
    )
    assert same.id == halt.id and changed is False  # a retry: nothing new

    new, changed = await halts.set_halt(
        db_session, scope="global", portfolio_id=None, reason="Nuovo motivo", by="agent:x"
    )
    assert changed and new.id != halt.id and new.reason == "Nuovo motivo"
    await db_session.refresh(halt)
    assert halt.active is False and halt.cleared_by == "agent:x" and halt.cleared_at

    cleared = await halts.clear_halt(db_session, scope="global", portfolio_id=None, by=ADMIN)
    assert cleared.id == new.id and cleared.active is False and cleared.cleared_by == ADMIN
    assert await halts.clear_halt(db_session, scope="global", portfolio_id=None, by=ADMIN) is None
    assert await halts.covering_halt(db_session, p.id) is None

    rows = list(await db_session.scalars(select(TradingHalt).order_by(TradingHalt.id)))
    assert [r.id for r in rows] == [halt.id, new.id]  # never deleted
    await db_session.flush()
    acts = list(await db_session.scalars(
        select(Activity).where(Activity.type.like("halt.%")).order_by(Activity.id)
    ))
    assert [(a.type, a.level, a.telegram) for a in acts] == [
        ("halt.set", "critical", "pending"), ("halt.set", "critical", "pending"),
        ("halt.cleared", "critical", "pending"),
    ]
    assert acts[0].portfolio_id is None and "Mercato instabile" in acts[0].summary
    assert acts[0].data["by"] == "admin" and acts[1].data["by"] == "agent:x"  # no email


async def test_reason_is_required_and_bounded(db_session, p):
    for bad in ("", "   ", "\x01\x02", "x" * 501, None, 5):
        with pytest.raises(SMTError) as exc:
            await halts.set_halt(db_session, scope="global", portfolio_id=None, reason=bad,
                                 by=ADMIN)
        assert exc.value.code == ErrorCode.INVALID_ORDER and exc.value.details["field"] == "reason"
    with pytest.raises(SMTError) as exc:
        await halts.set_halt(db_session, scope="all", portfolio_id=None, reason="x", by=ADMIN)
    assert exc.value.details["field"] == "scope"
    with pytest.raises(SMTError) as exc:
        await halts.set_halt(db_session, scope="portfolio", portfolio_id=None, reason="x",
                             by=ADMIN)
    assert exc.value.details["field"] == "portfolio_id"
    with pytest.raises(SMTError) as exc:
        await halts.set_halt(db_session, scope="portfolio", portfolio_id=uuid.uuid4(),
                             reason="x", by=ADMIN)
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_one_active_halt_per_target_in_the_database(db_session, p):
    await _halt(db_session, p)
    db_session.add(TradingHalt(scope="portfolio", portfolio_id=p.id, reason="b", set_by=ADMIN))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_portfolio_scope_covers_only_that_portfolio(db_session, p, qs):
    other = await _p(db_session)
    halt = await _halt(db_session, p, reason="Solo questo")
    assert (await halts.covering_halt(db_session, p.id)).id == halt.id
    assert await halts.covering_halt(db_session, other.id) is None
    err = await _refused(db_session, qs, _req(p))
    assert err.message == "Aperture sospese dal CEO: Solo questo"
    assert (await place_order(db_session, qs, _req(other), now=OPEN)).status == OrderStatus.filled


async def test_global_scope_covers_every_portfolio(db_session, p, qs):
    other = await _p(db_session)
    await _halt(db_session)
    await _refused(db_session, qs, _req(p))
    await _refused(db_session, qs, _req(other, "BTC-USD", qty=D("0.1")))


# --- pre-trade ----------------------------------------------------------------------------


async def test_opening_rejected_closing_allowed(db_session, p, qs):
    await place_order(db_session, qs, _req(p, qty=D(10)), now=OPEN)
    await _halt(db_session, p)

    await _refused(db_session, qs, _req(p, qty=D(1)))  # add
    await _refused(db_session, qs, _req(p, side=OrderSide.sell, qty=D(20)))  # flip to short
    await _refused(db_session, qs, _req(p, "BTC-USD", side=OrderSide.sell, qty=D("0.1")))  # short
    stored = await db_session.scalar(
        select(Order).where(Order.portfolio_id == p.id, Order.status == OrderStatus.rejected)
        .limit(1)
    )
    assert stored.reject_code == "TRADING_HALTED"

    partial = await place_order(db_session, qs, _req(p, side=OrderSide.sell, qty=D(4)), now=OPEN)
    assert partial.status == OrderStatus.filled
    closed = await close_position(db_session, qs, p.id, "AAPL", now=OPEN)
    assert (closed.qty, closed.status) == (D(6), OrderStatus.filled)
    assert await _held(db_session, p.id) == 0


async def test_bracket_entry_rejected(db_session, p, qs):
    await _halt(db_session, p)
    with pytest.raises(SMTError) as exc:
        await place_bracket_order(
            db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")),
            take_profit=D("210.00"), stop_loss=D("190.00"), now=OPEN,
        )
    assert exc.value.code == ErrorCode.TRADING_HALTED


async def test_oco_opening_legs_rejected(db_session, p, qs):
    await _halt(db_session, p)
    with pytest.raises(SMTError) as exc:
        await place_oco_order(
            db_session, qs, _req(p, type=OrderType.limit, limit_price=D("190.00")),
            _req(p, type=OrderType.stop, stop_price=D("210.00")), now=OPEN,
        )
    assert exc.value.code == ErrorCode.TRADING_HALTED


async def test_bracket_exits_of_an_existing_position_still_execute(db_session, p, qs):
    entry, tp, sl = await place_bracket_order(
        db_session, qs, _req(p), take_profit=D("220.00"), stop_loss=D("180.00"), now=OPEN,
    )
    assert entry.status == OrderStatus.filled
    await _halt(db_session)

    res = await _tick(db_session, qs, sl, "179.00", "179.10")  # stop loss triggers
    assert res is not None and res.order.status == OrderStatus.filled
    await db_session.refresh(tp)
    assert tp.status == OrderStatus.canceled
    assert await _held(db_session, p.id) == 0


async def test_resting_opening_order_refused_at_fill(db_session, p, qs):
    order = await place_order(
        db_session, qs, _req(p, type=OrderType.limit, limit_price=D("195.00")), now=OPEN
    )
    assert order.status == OrderStatus.pending
    await _halt(db_session, p, reason="Stop")
    with pytest.raises(SMTError) as exc:
        await _tick(db_session, qs, order, "194.90", "195.00")
    assert exc.value.code == ErrorCode.TRADING_HALTED
    assert exc.value.message == "Aperture sospese dal CEO: Stop"
    assert ErrorCode.TRADING_HALTED in REJECTING_CODES  # the processor rejects it


async def test_cleared_halt_lets_openings_through(db_session, p, qs):
    await _halt(db_session, p)
    await _refused(db_session, qs, _req(p))
    await halts.clear_halt(db_session, scope="portfolio", portfolio_id=p.id, by=ADMIN)
    assert (await place_order(db_session, qs, _req(p), now=OPEN)).status == OrderStatus.filled


async def test_kill_switch_wins_over_halt(db_session, p, qs):
    from smtrading.risk.settings import set_kill_switch

    await _halt(db_session, p)
    await set_kill_switch(db_session, True)
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, _req(p), now=OPEN)
    assert exc.value.code == ErrorCode.KILL_SWITCH_ACTIVE
