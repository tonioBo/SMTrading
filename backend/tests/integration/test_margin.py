import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    place_order,
    set_kill_switch_and_cancel,
)
from smtrading.events import POST_COMMIT_KEY
from smtrading.ledger.models import AccountKind, Journal, JournalType
from smtrading.ledger.service import get_balance, get_or_create_account, verify_chain
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.margin import check_portfolio_margin, liquidate, margin_sweep
from smtrading.risk.models import Notification
from smtrading.risk.settings import is_kill_switch_on
from smtrading.seed import run_seed
from smtrading.valuation.service import value_portfolio

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
TUE = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)  # Tuesday 11:00 Rome: MTA/XETRA open
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None)
    q.set_quote("SAP.DE", D("99.99"), D("100.01"), None)
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


async def _portfolio(session, leverage):
    return await create_portfolio(
        session, name=f"M-{uuid.uuid4().hex[:8]}", initial_cash=D(100000),
        leverage=D(leverage),
    )


async def _buy(session, qs, portfolio_id, symbol, qty):
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=OrderSide.buy, type=OrderType.market,
        qty=D(qty), status=OrderStatus.new,
    )
    session.add(o)
    await session.flush()
    quote = (await qs.get_quotes([symbol]))[symbol]
    assert await execute_fill(session, qs, o.id, quote=quote) is not None


async def _position(session, portfolio_id, symbol):
    acct = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=symbol, kind=AccountKind.position
    )
    return await get_balance(session, acct.id)


async def _notifications(session, portfolio_id, kind):
    return list(await session.scalars(
        select(Notification)
        .where(Notification.portfolio_id == portfolio_id, Notification.kind == kind)
    ))


def _events(session, type_):
    return [e for e in session.info.get(POST_COMMIT_KEY, []) if e[:2] == ("event", type_)]


async def test_leveraged_long_warns_then_liquidates(db_session, seeded, qs):
    p = await _portfolio(db_session, 2)
    await _buy(db_session, qs, p.id, "ENI.MI", 13000)  # ~195k exposure on ~100k equity
    assert await check_portfolio_margin(db_session, qs, p.id, now=TUE) == "ok"

    qs.set_quote("ENI.MI", D("10.995"), D("11.005"), None)  # equity ~48k < warn ~53.6k
    assert await check_portfolio_margin(db_session, qs, p.id, now=TUE) == "warning"
    assert len(await _notifications(db_session, p.id, "margin_warning")) == 1
    assert len(_events(db_session, "margin.warning")) == 1
    assert await _position(db_session, p.id, "ENI.MI") == D(13000)

    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)  # equity ~22k < maint 29.25k
    assert await check_portfolio_margin(db_session, qs, p.id, now=TUE) == "liquidated"
    assert await _position(db_session, p.id, "ENI.MI") == 0

    order = await db_session.scalar(
        select(Order).where(Order.portfolio_id == p.id, Order.source == "system")
    )
    assert order.status == OrderStatus.filled
    assert (order.side, order.type, order.qty) == (OrderSide.sell, OrderType.market, D(13000))
    assert order.avg_fill_price == D("8.995")
    journal = await db_session.scalar(select(Journal).where(Journal.order_id == order.id))
    assert journal.type == JournalType.LIQUIDATION
    assert len(await _notifications(db_session, p.id, "liquidation")) == 1
    assert len(_events(db_session, "liquidation")) == 1
    loan = await get_or_create_account(
        db_session, portfolio_id=p.id, asset="EUR", kind=AccountKind.margin_loan
    )
    assert await get_balance(db_session, loan.id) == 0
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "ok"
    assert (await verify_chain(db_session)).ok


async def test_liquidation_closes_worst_first_and_stops(db_session, seeded, qs):
    p = await _portfolio(db_session, 4)
    await _buy(db_session, qs, p.id, "ENI.MI", 20000)  # 300k
    await _buy(db_session, qs, p.id, "SAP.DE", 900)  # 90k
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "ok"

    qs.set_quote("ENI.MI", D("11.995"), D("12.005"), None)  # -60k
    qs.set_quote("SAP.DE", D("98.99"), D("99.01"), None)  # -0.9k
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "maintenance"

    closed = await liquidate(db_session, qs, p.id, now=TUE)
    assert [o.symbol for o in closed] == ["ENI.MI"]
    assert await _position(db_session, p.id, "ENI.MI") == 0
    assert await _position(db_session, p.id, "SAP.DE") == D(900)
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "ok"


async def test_liquidation_skips_a_symbol_without_quote(db_session, seeded, qs):
    p = await _portfolio(db_session, 4)
    await _buy(db_session, qs, p.id, "ENI.MI", 20000)
    await _buy(db_session, qs, p.id, "SAP.DE", 900)
    qs.set_quote("ENI.MI", D("11.995"), D("12.005"), None)

    valuation = await value_portfolio(db_session, qs, p.id)  # taken while ENI was quoted

    class _NoEni(FakeQuoteSource):
        """Every request including ENI.MI fails (the source is all-or-error)."""

        async def get_quotes(self, symbols, *, max_age_s=5):
            if "ENI.MI" in symbols:
                from smtrading.common.errors import ErrorCode, SMTError
                raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "down")
            return await super().get_quotes(symbols, max_age_s=max_age_s)

    flaky = _NoEni()
    flaky._quotes, flaky._fx = qs._quotes, qs._fx
    closed = await liquidate(db_session, flaky, p.id, now=TUE, valuation=valuation)
    assert [o.symbol for o in closed] == ["SAP.DE"]  # ENI skipped, the next one closed
    assert await _position(db_session, p.id, "ENI.MI") == D(20000)


async def test_liquidation_cancels_open_orders_of_the_symbol(db_session, seeded, qs):
    p = await _portfolio(db_session, 2)
    await _buy(db_session, qs, p.id, "ENI.MI", 13000)
    resting = Order(
        portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.sell, type=OrderType.limit,
        qty=D(13000), limit_price=D(20), status=OrderStatus.pending,
    )
    db_session.add(resting)
    await db_session.flush()
    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)
    assert await check_portfolio_margin(db_session, qs, p.id, now=TUE) == "liquidated"
    await db_session.refresh(resting)
    assert resting.status == OrderStatus.canceled


async def test_margin_warning_is_throttled(db_session, seeded, qs):
    p = await _portfolio(db_session, 2)
    await _buy(db_session, qs, p.id, "ENI.MI", 13000)
    qs.set_quote("ENI.MI", D("10.995"), D("11.005"), None)
    t0 = TUE
    assert await check_portfolio_margin(db_session, qs, p.id, now=t0) == "warning"
    assert await check_portfolio_margin(
        db_session, qs, p.id, now=t0 + timedelta(minutes=14)
    ) == "warning"
    assert len(await _notifications(db_session, p.id, "margin_warning")) == 1
    assert await check_portfolio_margin(
        db_session, qs, p.id, now=t0 + timedelta(minutes=16)
    ) == "warning"
    assert len(await _notifications(db_session, p.id, "margin_warning")) == 2
    assert len(_events(db_session, "margin.warning")) == 2


async def test_kill_switch_cancels_open_orders(db_session, seeded, qs):
    p = await _portfolio(db_session, 1)
    ids = []
    for price in ("90.00", "91.00"):
        o = await place_order(db_session, qs, PlaceOrderRequest(
            portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
            qty=D(1), limit_price=D(price),
        ), now=OPEN)
        assert o.status == OrderStatus.pending
        ids.append(o.id)

    assert await set_kill_switch_and_cancel(db_session, True, cancel_open=True) == 2
    assert await is_kill_switch_on(db_session)
    for oid in ids:
        assert (await db_session.get(Order, oid)).status == OrderStatus.canceled
    assert _events(db_session, "kill_switch")[-1][2] == {"on": True, "canceled": 2}

    assert await set_kill_switch_and_cancel(db_session, False, cancel_open=True) == 0
    assert not await is_kill_switch_on(db_session)


async def test_kill_switch_without_cancel_keeps_orders(db_session, seeded, qs):
    p = await _portfolio(db_session, 1)
    o = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(1), limit_price=D("90.00"),
    ), now=OPEN)
    assert await set_kill_switch_and_cancel(db_session, True, cancel_open=False) == 0
    assert (await db_session.get(Order, o.id)).status == OrderStatus.pending


# -- worker paths (real commits) ---------------------------------------------------------


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.aclose()


async def _leveraged_eni(maker, qs) -> uuid.UUID:
    async with maker() as s:
        p = await _portfolio(s, 2)
        await _buy(s, qs, p.id, "ENI.MI", 13000)
        await s.commit()
        return p.id


async def _system_orders(maker, pid) -> int:
    async with maker() as s:
        return await s.scalar(
            select(func.count()).select_from(Order)
            .where(Order.portfolio_id == pid, Order.source == "system")
        )


async def test_processor_checks_margin_after_tick(maker, redis, qs):
    pid = await _leveraged_eni(maker, qs)
    proc = OrderProcessor(maker, redis, qs, clock=lambda: TUE)
    await proc.sweep()  # learns the margin holdings

    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)
    proc.submit_tick("ENI.MI", qs._quotes["ENI.MI"])  # no orders on ENI, still kept
    await proc.drain()
    assert await _system_orders(maker, pid) == 1
    async with maker() as s:
        assert await _position(s, pid, "ENI.MI") == 0
    assert proc._margin == {}


async def test_processor_margin_check_is_throttled(maker, redis, qs):
    pid = await _leveraged_eni(maker, qs)
    clock = [TUE]
    proc = OrderProcessor(
        maker, redis, qs, clock=lambda: clock[0], min_eval_interval_s=0
    )
    await proc.sweep()
    qs.set_quote("ENI.MI", D("10.995"), D("11.005"), None)  # warning
    proc.submit_tick("ENI.MI", qs._quotes["ENI.MI"])
    await proc.drain()
    assert proc._margin_checked[pid] == clock[0]

    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)  # maintenance
    clock[0] += timedelta(seconds=1)
    proc.submit_tick("ENI.MI", qs._quotes["ENI.MI"])
    wait = await proc.drain()
    assert await _system_orders(maker, pid) == 0  # within 2s of the last check
    assert wait == pytest.approx(1.0)

    assert proc._margin_dirty == {"ENI.MI"}  # throttled, not dropped

    clock[0] += timedelta(seconds=1)
    assert await proc.drain() is None  # due now, without a new tick
    assert await _system_orders(maker, pid) == 1
    assert proc._margin_dirty == set()


async def test_margin_sweep_liquidates(maker, redis, qs):
    pid = await _leveraged_eni(maker, qs)
    async with maker() as s:  # an unleveraged portfolio is not margin-checked
        other = await _portfolio(s, 1)
        await _buy(s, qs, other.id, "SAP.DE", 10)
        await s.commit()
    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)
    await margin_sweep(maker, qs, redis, clock=lambda: TUE)
    assert await _system_orders(maker, pid) == 1
    assert await _system_orders(maker, other.id) == 0


async def test_liquidation_skips_closed_market_and_stale_quotes(db_session, seeded, qs):
    """Ruling R19: Saturday crypto crash with a (worse) US equity position."""
    p = await _portfolio(db_session, 4)
    qs.set_quote("AAPL", D("199.99"), D("200.01"), None, ts=SATURDAY)
    qs.set_quote("BTC-USD", D("49999"), D("50001"), None, ts=SATURDAY)
    await _buy(db_session, qs, p.id, "AAPL", 1000)  # 200k USD
    await _buy(db_session, qs, p.id, "BTC-USD", 4)  # 200k USD
    friday_close = SATURDAY - timedelta(hours=19)
    qs.set_quote("AAPL", D("139.99"), D("140.01"), None, ts=friday_close)  # -48k EUR
    qs.set_quote("BTC-USD", D("41999"), D("42001"), None, ts=SATURDAY)  # -25.6k EUR
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "maintenance"

    assert await check_portfolio_margin(db_session, qs, p.id, now=SATURDAY) == "liquidated"
    assert await _position(db_session, p.id, "AAPL") == D(1000)  # NASDAQ closed: skipped
    assert await _position(db_session, p.id, "BTC-USD") == 0
    (note,) = await _notifications(db_session, p.id, "liquidation")
    assert note.payload["skipped"] == ["AAPL"]
    assert [o["symbol"] for o in note.payload["orders"]] == ["BTC-USD"]


async def test_stale_quote_is_not_liquidated_and_warns(db_session, seeded, qs):
    p = await _portfolio(db_session, 2)
    await _buy(db_session, qs, p.id, "ENI.MI", 13000)
    # eu_equity stale threshold 1200s; this quote is 30 minutes old
    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None, ts=TUE - timedelta(minutes=30))
    assert await check_portfolio_margin(db_session, qs, p.id, now=TUE) == "warning"
    assert await _position(db_session, p.id, "ENI.MI") == D(13000)
    assert await _system_orders_in(db_session, p.id) == 0
    (note,) = await _notifications(db_session, p.id, "margin_warning")
    assert note.payload["skipped"] == ["ENI.MI"]


async def _system_orders_in(session, pid) -> int:
    return await session.scalar(
        select(func.count()).select_from(Order)
        .where(Order.portfolio_id == pid, Order.source == "system")
    )


async def test_kill_switch_catches_an_order_placed_concurrently(maker, qs):
    """M2: a placement holding its portfolio lock when the switch is turned on is
    canceled once it commits (the portfolio was not listed as having open orders)."""
    import asyncio

    async with maker() as s:
        p = await _portfolio(s, 1)
        await s.commit()
    placing = maker()
    order = await place_order(placing, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(1), limit_price=D("90.00"),
    ), now=OPEN)  # holds the portfolio row lock, not committed yet

    async def switch_on() -> int:
        async with maker() as s:
            n = await set_kill_switch_and_cancel(s, True, cancel_open=True)
            await s.commit()
            return n

    task = asyncio.create_task(switch_on())
    await asyncio.sleep(0.5)
    assert not task.done()  # waiting for the portfolio lock
    await placing.commit()
    await placing.close()
    assert await asyncio.wait_for(task, 10) == 1
    async with maker() as s:
        assert (await s.get(Order, order.id)).status == OrderStatus.canceled


# -- residual fix: liquidation memo prefetches the fee/funding fx of every close --------


async def test_liquidation_closes_a_usd_position_after_an_eur_one(db_session, seeded, qs):
    """EUR cash <= fee: the AAPL close converts its EUR fee into USD (R9) after the memo
    was frozen by the ENI.MI close, so EUR->USD must have been prefetched."""
    p = await _portfolio(db_session, 4)
    await _buy(db_session, qs, p.id, "ENI.MI", 20000)  # 300k EUR
    await _buy(db_session, qs, p.id, "AAPL", 800)  # 80k USD
    # -110k: the ENI proceeds (~190k) only repay the EUR loan (~200k), EUR cash stays 0
    qs.set_quote("ENI.MI", D("9.495"), D("9.505"), None)
    assert (await value_portfolio(db_session, qs, p.id)).margin_level() == "maintenance"

    closed = await liquidate(db_session, qs, p.id, now=OPEN)

    assert [o.symbol for o in closed] == ["ENI.MI", "AAPL"]
    assert all(o.status == OrderStatus.filled for o in closed)
    assert await _position(db_session, p.id, "ENI.MI") == 0
    assert await _position(db_session, p.id, "AAPL") == 0
    rejected = await db_session.scalar(
        select(func.count()).select_from(Order)
        .where(Order.portfolio_id == p.id, Order.status == OrderStatus.rejected)
    )
    assert rejected == 0
