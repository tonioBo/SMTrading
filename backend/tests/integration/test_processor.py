import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode
from smtrading.engine import processor as processor_module
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import dispatch_post_commit
from smtrading.marketdata import serialization
from smtrading.marketdata.streaming import TRACKED_KEY
from smtrading.marketdata.types import Quote
from smtrading.portfolios.service import create_portfolio, withdraw
from smtrading.risk.models import Notification
from smtrading.seed import run_seed

D = Decimal
TEN = Decimal(10)
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
NASDAQ_CLOSE_TUE = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
FRIDAY_LAST = datetime(2026, 11, 13, 20, 59, tzinfo=UTC)
MONDAY_OPEN = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)  # NASDAQ opens 9:30 EST


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


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


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))  # fresh for placement
    return q


def _proc(maker, redis, qs, clock, **kw):
    return OrderProcessor(maker, redis, qs, clock=clock, **kw)


async def _portfolio(maker, cash=100000, slippage_bps=0):
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"P-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), leverage=D(1),
            slippage_bps=slippage_bps,
        )
        await s.commit()
        return p.id


async def _place(maker, redis, qs, pid, *, now=OPEN, symbol="AAPL", side=OrderSide.buy,
                 type=OrderType.market, qty=TEN, **kw):
    async with maker() as s:
        order = await place_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol=symbol, side=side, type=type, qty=qty, **kw
            ), now=now,
        )
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order(maker, order_id) -> Order:
    async with maker() as s:
        return await s.get(Order, order_id)


async def _fills(maker, order_id) -> list[Fill]:
    async with maker() as s:
        return list(await s.scalars(select(Fill).where(Fill.order_id == order_id)))


def _tick(qs, bid, ask, ts):
    qs.set_quote("AAPL", D(bid), D(ask), None, ts=ts)
    return qs._quotes["AAPL"]


async def test_limit_buy_fills_on_tick_and_untracks(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    assert (await _order(maker, oid)).status == OrderStatus.pending
    assert await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)

    assert await proc.on_tick("AAPL", _tick(qs, "96.00", "96.10", clock.now)) == 0
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "94.90", "95.00", clock.now)) == 1

    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("95.00")
    assert len(await _fills(maker, oid)) == 1
    assert not await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")


async def test_on_tick_is_throttled_per_symbol(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock, min_eval_interval_s=0.25)

    assert await proc.on_tick("AAPL", _tick(qs, "96.00", "96.10", clock.now)) == 0
    clock.advance(0.1)
    assert await proc.on_tick("AAPL", _tick(qs, "94.90", "95.00", clock.now)) == 0  # throttled
    assert (await _order(maker, oid)).status == OrderStatus.pending
    clock.advance(0.2)
    assert await proc.on_tick("AAPL", _tick(qs, "94.90", "95.00", clock.now)) == 1


async def test_stop_sell_fills_at_bid_not_stop(maker, redis, qs):
    pid = await _portfolio(maker, slippage_bps=10)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    assert (await _order(maker, oid)).status == OrderStatus.pending
    clock = Clock(OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)

    assert await proc.on_tick("AAPL", _tick(qs, "90.00", "90.10", clock.now)) == 1

    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled
    # bid 90 * (1 - 10/10000) = 89.91, rounded down to the tick: far below the stop
    assert order.avg_fill_price == D("89.91")


async def test_sweep_fills_pending_stop_at_current_price_after_restart(maker, redis, qs):
    """Review Focus 5: a stop crossed while the worker was down fills at the current
    (worse) price on the first sweep."""
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=5))
    _tick(qs, "90.00", "90.10", clock.now)

    await _proc(maker, redis, qs, clock).sweep()

    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("90.00")


async def test_trailing_stop_updates_reference_then_fires(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.trailing_stop,
        trail_pct=D(5),
    )
    assert (await _order(maker, oid)).trail_ref_price == D("99.90")
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)

    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "101.00", "101.10", clock.now)) == 0
    assert (await _order(maker, oid)).trail_ref_price == D("101.00")
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "103.00", "103.10", clock.now)) == 0
    order = await _order(maker, oid)
    assert order.trail_ref_price == D("103.00") and order.status == OrderStatus.pending
    clock.advance(1)
    # stop = 103 * 0.95 = 97.85
    assert await proc.on_tick("AAPL", _tick(qs, "97.80", "97.90", clock.now)) == 1
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("97.80")


async def test_trailing_stop_without_reference_initialises_it_first(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.trailing_stop,
        trail_amount=D("5.00"),
    )
    async with maker() as s:  # as if placed on a closed market with no quote
        await s.execute(update(Order).where(Order.id == oid).values(trail_ref_price=None))
        await s.commit()
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)

    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "90.00", "90.10", clock.now)) == 0
    order = await _order(maker, oid)
    assert order.trail_ref_price == D("90.00") and order.status == OrderStatus.pending
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "84.90", "85.00", clock.now)) == 1
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_stop_limit_triggers_then_fills_as_limit(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop_limit,
        stop_price=D("95.00"), limit_price=D("94.00"),
    )
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)

    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "93.00", "93.10", clock.now)) == 1
    assert (await _order(maker, oid)).status == OrderStatus.triggered
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "93.50", "93.60", clock.now)) == 0
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "94.20", "94.30", clock.now)) == 1
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("94.20")


async def test_queued_weekend_order_fills_at_monday_open(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    assert (await _order(maker, oid)).status == OrderStatus.queued
    clock = Clock(SATURDAY)
    proc = _proc(maker, redis, qs, clock)

    _tick(qs, "99.90", "100.00", FRIDAY_LAST)
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.queued  # market closed

    clock.now = MONDAY_OPEN + timedelta(minutes=1)
    _tick(qs, "101.90", "102.00", clock.now)
    await proc.sweep()

    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("102.00")


async def test_queued_order_waits_for_fresh_quote_then_rejects_stale(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    _tick(qs, "99.90", "100.00", FRIDAY_LAST)  # Friday's last quote, never refreshed
    clock = Clock(MONDAY_OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)

    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.queued  # waits

    clock.now = MONDAY_OPEN + timedelta(minutes=11)
    await proc.sweep()

    order = await _order(maker, oid)
    assert order.status == OrderStatus.rejected
    assert order.reject_code == ErrorCode.STALE_QUOTE
    assert not await _fills(maker, oid)
    async with maker() as s:
        notes = list(await s.scalars(select(Notification).where(Notification.portfolio_id == pid)))
    assert [n.kind for n in notes] == ["order.rejected"]
    assert notes[0].payload["code"] == "STALE_QUOTE"
    assert not await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")


async def test_day_order_expires_at_sweep_after_close(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(
        maker, redis, qs, pid, type=OrderType.limit, limit_price=D("90.00"),
        tif=TimeInForce.day,
    )
    assert (await _order(maker, oid)).expires_at == NASDAQ_CLOSE_TUE
    clock = Clock(NASDAQ_CLOSE_TUE - timedelta(seconds=1))
    proc = _proc(maker, redis, qs, clock)

    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.pending
    clock.advance(2)
    await proc.sweep()

    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert not await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")


async def test_closed_market_does_not_evaluate_resting_orders(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(SATURDAY)
    proc = _proc(maker, redis, qs, clock)

    assert await proc.on_tick("AAPL", _tick(qs, "90.00", "90.10", clock.now)) == 0
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.pending


async def test_concurrent_on_tick_single_fill(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(OPEN)
    procs = [_proc(maker, redis, qs, clock) for _ in range(2)]
    quote = _tick(qs, "94.90", "95.00", clock.now)

    results = await asyncio.gather(*(p.on_tick("AAPL", quote) for p in procs))

    assert sorted(results) == [0, 1]
    assert len(await _fills(maker, oid)) == 1
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_smt_error_rejects_order_and_next_order_still_fills(maker, redis, qs):
    poor, rich = await _portfolio(maker), await _portfolio(maker)
    first = await _place(maker, redis, qs, poor, type=OrderType.limit, limit_price=D("95.00"))
    second = await _place(maker, redis, qs, rich, type=OrderType.limit, limit_price=D("95.00"))
    async with maker() as s:  # the cash leaves after placement: no buying power at the fill
        await withdraw(s, poor, D(99900), "EUR", author_id=None)
        await s.commit()
    clock = Clock(OPEN)

    n = await _proc(maker, redis, qs, clock).on_tick(
        "AAPL", _tick(qs, "94.90", "95.00", clock.now)
    )

    assert n == 1
    rejected = await _order(maker, first)
    assert rejected.status == OrderStatus.rejected
    assert rejected.reject_code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert (await _order(maker, second)).status == OrderStatus.filled
    async with maker() as s:
        kinds = list(await s.scalars(
            select(Notification.kind).where(Notification.portfolio_id == poor)
        ))
    assert kinds == ["order.rejected"]


async def test_unexpected_error_is_logged_and_does_not_stop_next_order(
    maker, redis, qs, monkeypatch, caplog
):
    pid = await _portfolio(maker)
    first = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    second = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("94.50"))
    real = processor_module.execute_fill

    async def flaky(session, qs_, order_id, **kw):
        if order_id == first:
            raise RuntimeError("boom")
        return await real(session, qs_, order_id, **kw)

    monkeypatch.setattr(processor_module, "execute_fill", flaky)
    clock = Clock(OPEN)

    with caplog.at_level(logging.ERROR, logger="smtrading.engine.processor"):
        n = await _proc(maker, redis, qs, clock).on_tick(
            "AAPL", _tick(qs, "94.40", "94.50", clock.now)
        )

    assert n == 1
    assert (await _order(maker, first)).status == OrderStatus.pending  # rolled back, retried later
    assert (await _order(maker, second)).status == OrderStatus.filled
    assert any(r.exc_info and "boom" in str(r.exc_info[1]) for r in caplog.records)


async def test_run_consumes_ticks_from_redis(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    proc = _proc(maker, redis, qs, Clock(OPEN), sweep_every_s=3600)
    task = asyncio.create_task(proc.run())
    try:
        # only the tick carries the crossing price; the quote source still says 100
        quote = Quote("AAPL", D("94.90"), D("95.00"), None, OPEN, "fake")
        for _ in range(100):
            await redis.publish("ticks:AAPL", serialization.dumps(quote))
            if (await _order(maker, oid)).status == OrderStatus.filled:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await _order(maker, oid)).status == OrderStatus.filled
    async with maker() as s:
        assert await s.scalar(
            select(func.count()).select_from(Fill).where(Fill.order_id == oid)
        ) == 1
