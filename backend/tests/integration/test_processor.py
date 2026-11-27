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
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine import processor as processor_module
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_bracket_order, place_order
from smtrading.events import dispatch_post_commit
from smtrading.marketdata import serialization
from smtrading.marketdata.streaming import TRACKED_KEY
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio, withdraw
from smtrading.risk.models import Notification
from smtrading.risk.settings import set_kill_switch
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


class FlakyFxQuoteSource(FakeQuoteSource):
    def __init__(self) -> None:
        super().__init__()
        self.fx_down = False
        self.quote_calls: list[list[str]] = []

    async def get_fx_rate(self, base, quote):
        if self.fx_down and base.upper() != quote.upper():
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "fx down")
        return await super().get_fx_rate(base, quote)

    async def get_quotes(self, symbols, *, max_age_s=5):
        self.quote_calls.append(list(symbols))
        return await super().get_quotes(symbols, max_age_s=max_age_s)


@pytest.fixture
def flaky_qs():
    q = FlakyFxQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    q.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    return q


async def test_rejected_bracket_entry_cancels_held_children_and_untracks(maker, redis, qs):
    pid = await _portfolio(maker)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
                qty=TEN, limit_price=D("95.00"),
            ), take_profit=D("110.00"), stop_loss=D("90.00"), now=OPEN,
        )
        await s.commit()
        await dispatch_post_commit(s, redis)
    assert (entry.status, tp.status, sl.status) == (
        OrderStatus.pending, OrderStatus.held, OrderStatus.held
    )
    async with maker() as s:
        await withdraw(s, pid, D(99900), "EUR", author_id=None)
        await s.commit()
    clock = Clock(OPEN)

    assert await _proc(maker, redis, qs, clock).on_tick(
        "AAPL", _tick(qs, "94.90", "95.00", clock.now)
    ) == 0

    assert (await _order(maker, entry.id)).status == OrderStatus.rejected
    assert (await _order(maker, tp.id)).status == OrderStatus.canceled
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled
    assert not await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")


async def test_provider_error_leaves_order_pending_and_next_pass_fills(maker, redis, flaky_qs):
    qs = flaky_qs
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)
    _tick(qs, "94.90", "95.00", clock.now)

    qs.fx_down = True
    await proc.sweep()
    order = await _order(maker, oid)
    assert order.status == OrderStatus.pending and order.reject_code is None
    assert not await _fills(maker, oid)

    qs.fx_down = False
    clock.advance(1)
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_ticks_are_coalesced_latest_wins(maker, redis, qs, monkeypatch):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)
    await proc.sweep()  # learns the active symbols
    seen = []
    real = proc.on_tick

    async def spy(symbol, quote):
        seen.append((symbol, quote.ask))
        return await real(symbol, quote)

    monkeypatch.setattr(proc, "on_tick", spy)
    for i in range(10):  # crossing early, not at the end
        proc.submit_tick("AAPL", Quote("AAPL", D(94 + i), D(94 + i) + D("0.10"), None,
                                       clock.now, "fake"))
    proc.submit_tick("MSFT", Quote("MSFT", D(1), D(2), None, clock.now, "fake"))  # no orders

    assert await proc.drain() is None

    assert seen == [("AAPL", D("103.10"))]
    assert (await _order(maker, oid)).status == OrderStatus.pending

    # a new tick inside the throttle window waits; it runs once the window has passed
    proc.submit_tick("AAPL", Quote("AAPL", D("94.90"), D("95.00"), None, clock.now, "fake"))
    wait = await proc.drain()
    assert wait is not None and 0 < wait <= 0.25
    assert len(seen) == 1
    clock.advance(0.3)
    assert await proc.drain() is None
    assert seen[-1] == ("AAPL", D("95.00"))
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_queued_order_without_any_quote_rejected_after_open(maker, redis, flaky_qs):
    qs = flaky_qs
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    del qs._quotes["AAPL"]
    clock = Clock(MONDAY_OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)

    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.queued

    clock.now = MONDAY_OPEN + timedelta(minutes=11)
    await proc.sweep()
    order = await _order(maker, oid)
    assert order.status == OrderStatus.rejected and order.reject_code == ErrorCode.STALE_QUOTE


async def test_failing_symbol_is_not_refetched_for_60s(maker, redis, flaky_qs):
    qs = flaky_qs
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    await _place(maker, redis, qs, pid, symbol="BTC-USD", type=OrderType.limit,
                 qty=D("0.1"), limit_price=D("40000.00"))
    del qs._quotes["AAPL"]
    qs.quote_calls.clear()  # placement fetched quotes too
    clock = Clock(OPEN)
    proc = _proc(maker, redis, qs, clock)

    await proc.sweep()
    assert qs.quote_calls == [["AAPL", "BTC-USD"], ["AAPL"], ["BTC-USD"]]
    qs.quote_calls.clear()
    clock.advance(30)
    await proc.sweep()
    assert qs.quote_calls == [["BTC-USD"]]
    qs.quote_calls.clear()
    clock.advance(31)
    await proc.sweep()
    assert qs.quote_calls[0] == ["AAPL", "BTC-USD"]


async def test_order_past_expiry_is_not_filled_by_tick(maker, redis, flaky_qs):
    qs = flaky_qs
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, symbol="BTC-USD", type=OrderType.limit,
                       qty=D("0.1"), limit_price=D("49000.00"), tif=TimeInForce.day)
    expires = (await _order(maker, oid)).expires_at
    assert expires == datetime(2026, 11, 10, 23, 0, tzinfo=UTC)  # midnight Europe/Rome
    clock = Clock(expires + timedelta(seconds=30))
    proc = _proc(maker, redis, qs, clock)

    qs.set_quote("BTC-USD", D("48000.00"), D("48010.00"), None, ts=clock.now)
    assert await proc.on_tick("BTC-USD", qs._quotes["BTC-USD"]) == 0
    assert (await _order(maker, oid)).status == OrderStatus.pending

    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert not await _fills(maker, oid)


# -- I3: portfolio status and kill switch enforced again at fill time ------------------


async def _set_status(maker, pid, status):
    async with maker() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid).values(status=status))
        await s.commit()


@pytest.mark.parametrize("status, code", [
    (PortfolioStatus.locked, ErrorCode.PORTFOLIO_LOCKED),
    (PortfolioStatus.reduce_only, ErrorCode.REDUCE_ONLY),
])
async def test_resting_buy_rejected_at_fill_by_portfolio_status(maker, redis, qs, status, code):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    await _set_status(maker, pid, status)  # after placement: only the fill can catch it
    clock = Clock(OPEN)

    await _proc(maker, redis, qs, clock).on_tick("AAPL", _tick(qs, "94.90", "95.00", clock.now))

    order = await _order(maker, oid)
    assert order.status == OrderStatus.rejected and order.reject_code == code
    assert await _fills(maker, oid) == []


async def test_reduce_only_portfolio_still_fills_a_reducing_stop(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                       stop_price=D("90.00"))
    await _set_status(maker, pid, PortfolioStatus.reduce_only)
    clock = Clock(OPEN)

    await _proc(maker, redis, qs, clock).on_tick("AAPL", _tick(qs, "89.90", "90.00", clock.now))

    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_kill_switch_rejects_an_exposure_increasing_fill(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    buy = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    sell = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.limit,
                        limit_price=D("95.00"), qty=D(5))
    async with maker() as s:  # switched on without canceling the resting orders
        await set_kill_switch(s, True)
        await s.commit()
    clock = Clock(OPEN)

    await _proc(maker, redis, qs, clock).on_tick("AAPL", _tick(qs, "95.00", "95.00", clock.now))

    rejected = await _order(maker, buy)
    assert rejected.status == OrderStatus.rejected
    assert rejected.reject_code == ErrorCode.KILL_SWITCH_ACTIVE
    assert (await _order(maker, sell)).status == OrderStatus.filled  # reducing: allowed


async def test_reduce_only_order_with_nothing_to_reduce_is_canceled_by_processor(
    maker, redis, qs
):
    pid = await _portfolio(maker)
    async with maker() as s:
        order = Order(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.sell, type=OrderType.stop,
            qty=TEN, stop_price=D("90.00"), status=OrderStatus.pending, reduce_only=True,
        )
        s.add(order)
        await s.commit()
    clock = Clock(OPEN)

    assert await _proc(maker, redis, qs, clock).on_tick(
        "AAPL", _tick(qs, "89.90", "90.00", clock.now)
    ) == 0

    order = await _order(maker, order.id)
    assert order.status == OrderStatus.canceled and order.reject_message == "nothing_to_reduce"
    assert await _fills(maker, order.id) == []


# -- plan 16 Task A: valid_until --------------------------------------------------------

VALID_UNTIL = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


async def _bracket(maker, redis, qs, pid, **kw):
    async with maker() as s:
        orders = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
                qty=TEN, limit_price=D("95.00"), **kw,
            ), take_profit=D("110.00"), stop_loss=D("90.00"), now=OPEN,
        )
        await s.commit()
        await dispatch_post_commit(s, redis)
    return [o.id for o in orders]


async def _activity_types(maker, order_id) -> list[str]:
    from smtrading.activity.models import Activity

    async with maker() as s:
        return list(await s.scalars(
            select(Activity.type).where(Activity.data["order_id"].astext == str(order_id))
            .order_by(Activity.id)))


async def test_valid_until_expires_an_unfilled_order(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("90.00"),
                       valid_until=VALID_UNTIL.isoformat())
    clock = Clock(VALID_UNTIL - timedelta(seconds=1))
    proc = _proc(maker, redis, qs, clock)
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.pending
    clock.advance(2)
    # a tick past valid_until never fills it, the sweep expires it
    assert await proc.on_tick("AAPL", _tick(qs, "89.00", "89.10", clock.now)) == 0
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert await _fills(maker, oid) == []
    assert "order.expired" in await _activity_types(maker, oid)


async def test_valid_until_expired_entry_cancels_its_held_exits(maker, redis, qs):
    pid = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid, valid_until=VALID_UNTIL)
    await _proc(maker, redis, qs, Clock(VALID_UNTIL)).sweep()
    assert (await _order(maker, entry)).status == OrderStatus.expired
    assert (await _order(maker, tp)).status == OrderStatus.canceled
    assert (await _order(maker, sl)).status == OrderStatus.canceled
    assert not await redis.sismember(f"{TRACKED_KEY}:orders", "AAPL")


async def test_valid_until_does_not_touch_the_exits_of_a_filled_entry(maker, redis, qs):
    pid = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid, valid_until=VALID_UNTIL)
    clock = Clock(OPEN + timedelta(minutes=5))
    proc = _proc(maker, redis, qs, clock)
    assert await proc.on_tick("AAPL", _tick(qs, "94.90", "95.00", clock.now)) == 1
    clock.now = VALID_UNTIL + timedelta(hours=1)
    qs.set_quote("AAPL", D("99.90"), D("100.00"), None, ts=clock.now)
    await proc.sweep()
    assert (await _order(maker, entry)).status == OrderStatus.filled
    for child in (tp, sl):  # gtc exits keep protecting the position
        o = await _order(maker, child)
        assert o.status == OrderStatus.pending and o.expires_at is None


async def test_valid_until_partially_filled_entry_keeps_exits_for_the_filled_qty(
    maker, redis, qs,
):
    """Simulated fills are whole, so a partly filled local entry should not exist; if one
    does, its remainder expires and its exits are released for the filled quantity (as
    BrokerSync does for a broker entry), never canceled."""
    pid = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid, valid_until=VALID_UNTIL)
    async with maker() as s:
        await s.execute(update(Order).where(Order.id == entry).values(
            status=OrderStatus.partially_filled, filled_qty=D(4)))
        await s.commit()
    await _proc(maker, redis, qs, Clock(VALID_UNTIL)).sweep()
    e = await _order(maker, entry)
    assert e.status == OrderStatus.expired and e.filled_qty == 4
    for child in (tp, sl):
        o = await _order(maker, child)
        assert o.status == OrderStatus.pending and o.qty == 4


async def test_valid_until_expires_with_the_kill_switch_on(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("90.00"),
                       valid_until=VALID_UNTIL)
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await _proc(maker, redis, qs, Clock(VALID_UNTIL)).sweep()
    assert (await _order(maker, oid)).status == OrderStatus.expired


async def test_valid_until_queued_market_order_expires_before_the_open(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY,
                       valid_until=MONDAY_OPEN - timedelta(minutes=30))
    assert (await _order(maker, oid)).status == OrderStatus.queued
    clock = Clock(MONDAY_OPEN + timedelta(minutes=1))
    qs.set_quote("AAPL", D("99.90"), D("100.00"), None, ts=clock.now)
    await _proc(maker, redis, qs, clock).sweep()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert await _fills(maker, oid) == []


# -- plan 25: suspect quotes (quality guard) ------------------------------------------------

def _suspect_tick(qs, bid, ask, ts, reason="jump"):
    qs.set_quote("AAPL", D(bid), D(ask), None, ts=ts, suspect=reason)
    return qs._quotes["AAPL"]


async def _activities(maker, kind):
    from smtrading.activity.models import Activity

    async with maker() as s:
        return list(await s.scalars(select(Activity).where(Activity.type == kind)))


async def test_stop_never_fires_on_a_suspect_quote(maker, redis, qs):
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock, min_eval_interval_s=0)

    assert await proc.on_tick("AAPL", _suspect_tick(qs, "60.00", "60.10", clock.now)) == 0
    clock.advance(1)
    assert await proc.on_tick("AAPL", _suspect_tick(qs, "60.00", "60.10", clock.now)) == 0
    order = await _order(maker, oid)
    assert order.status == OrderStatus.pending and not await _fills(maker, oid)
    acts = await _activities(maker, "order.quote_suspect")
    assert len(acts) == 1  # once per order and hour
    assert acts[0].portfolio_id == pid and "prezzo da verificare" in acts[0].summary
    assert "salto improvviso" in acts[0].summary

    clock.advance(1)  # the next good quote: the stop fires there
    assert await proc.on_tick("AAPL", _tick(qs, "94.00", "94.10", clock.now)) == 1
    assert (await _order(maker, oid)).avg_fill_price == D("94.00")


async def test_limit_and_sweep_wait_on_a_suspect_quote(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    clock = Clock(OPEN + timedelta(minutes=1))
    _suspect_tick(qs, "50.00", "50.10", clock.now, reason="disagree")
    await _proc(maker, redis, qs, clock).sweep()
    assert (await _order(maker, oid)).status == OrderStatus.pending
    assert not await _fills(maker, oid)


async def test_queued_market_order_waits_on_suspect_instead_of_rejecting(maker, redis, qs):
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    clock = Clock(MONDAY_OPEN + timedelta(minutes=11))
    _suspect_tick(qs, "99.90", "100.00", clock.now)
    await _proc(maker, redis, qs, clock).sweep()
    assert (await _order(maker, oid)).status == OrderStatus.queued
    _tick(qs, "99.90", "100.00", clock.now)
    await _proc(maker, redis, qs, clock).sweep()
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_genuine_gap_on_a_lone_stream_fires_the_stop_in_bounded_time(
        maker, redis, qs, fresh_db, redis_url, monkeypatch):
    """Money safety: a real -20% gap seen by one source only (stream ticks through the
    quality guard) is suspect at first, yet the stop fires within CONFIRM_JUMP_S (plus
    one tick); the owner is warned on Telegram if a symbol stays suspect longer."""
    from smtrading.marketdata import quality
    from smtrading.marketdata.streaming import track
    from smtrading.marketdata.suspect_alarm import SuspectAlarm
    from smtrading.telegram.models import TelegramOutbox

    for k, v in {"SMT_DATABASE_URL": fresh_db, "SMT_REDIS_URL": redis_url,
                 "SMT_MASTER_KEY": "test-master-key", "SMT_ADMIN_EMAIL": "admin@example.com",
                 "SMT_ADMIN_PASSWORD": "test-password"}.items():
        monkeypatch.setenv(k, v)
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=1))
    guard = quality.QuoteGuard(redis, clock=lambda: clock.now.timestamp())
    proc = _proc(maker, redis, qs, clock, min_eval_interval_s=0)

    async def tick(price: str) -> int:
        raw = Quote("AAPL", D(price), D(price) + D("0.05"), None, clock.now, "fake")
        q = await guard.check_stream(raw, "us_equity")
        qs._quotes["AAPL"] = q
        return await proc.on_tick("AAPL", q)

    assert await tick("100.00") == 0  # the accepted reference
    gap_at = clock.now
    fired = None
    for _ in range(20):
        clock.advance(20)
        if await tick("80.00"):
            fired = clock.now
            break
    assert fired is not None
    assert (fired - gap_at).total_seconds() <= quality.CONFIRM_JUMP_S + 40
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("80.00")

    # a symbol held and still suspect after 15 minutes: one Telegram warning
    await track(redis, ["MSFT"], source="positions")
    await redis.set(quality.SINCE_KEY.format("MSFT"),
                    str(clock.now.timestamp() - quality.SUSPECT_ALERT_AFTER_S - 60))
    alarm = SuspectAlarm(maker, redis, clock=lambda: clock.now.timestamp())
    assert await alarm.tick() == ["MSFT"]
    async with maker() as s:
        texts = list(await s.scalars(select(TelegramOutbox.text)))
    assert any("MSFT" in t and "Prezzo non affidabile" in t for t in texts)


def _wide(qs, bid, ask, last, ts, provider="fake"):
    qs.set_quote("AAPL", D(bid), D(ask), None if last is None else D(last), ts=ts,
                 provider=provider)
    return qs._quotes["AAPL"]


async def test_a_wide_spread_bid_does_not_trigger_a_stop(maker, redis, qs):
    """24/11 15:30: at the US open the first quote of MSFT was bid 470.77 / ask 521.51
    (last 489.80); the stop at 478.33 fired on that bid and sold at 490.47. With a spread
    over MAX_SPREAD the last trade decides, and without one the order waits."""
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)
    assert await proc.on_tick("AAPL", _wide(qs, "90.00", "110.00", "100.00", clock.now)) == 0
    clock.advance(1)
    assert await proc.on_tick("AAPL", _wide(qs, "90.00", "110.00", None, clock.now)) == 0
    assert (await _order(maker, oid)).status == OrderStatus.pending
    clock.advance(1)
    # the real price crosses the stop: the last trade under it
    assert await proc.on_tick("AAPL", _wide(qs, "90.00", "110.00", "94.00", clock.now)) == 1


async def test_a_stop_triggered_on_a_wide_book_fills_at_the_last_trade(maker, redis, qs):
    """The last trade triggers the stop on a book wider than MAX_SPREAD: the fill is priced
    on that last trade too, never on the wide bid (27/11 JPM: bid 305.04 / last 324.17)."""
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)
    assert await proc.on_tick(
        "AAPL", _wide(qs, "90.00", "110.00", "94.00", clock.now, "alpaca")) == 1
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled
    assert D("93.50") < order.avg_fill_price <= D("94.00")


async def test_another_providers_wide_book_fills_on_its_bid_as_before(maker, redis, qs):
    """Only Alpaca's IEX book is a ghost: elsewhere the last trade triggers, the bid fills."""
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)  # long 10
    oid = await _place(
        maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("95.00")
    )
    clock = Clock(OPEN + timedelta(minutes=1))
    proc = _proc(maker, redis, qs, clock)
    assert await proc.on_tick(
        "AAPL", _wide(qs, "90.00", "110.00", "94.00", clock.now, "kraken")) == 1
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled and order.avg_fill_price == D("90.00")


async def test_a_market_order_on_a_wide_book_fills_at_the_last_trade(maker, redis, qs):
    pid = await _portfolio(maker)
    # fresh for the risk check (wall clock), placed in an open session (OPEN)
    qs.set_quote("AAPL", D("90.00"), D("110.00"), D("100.00"), provider="alpaca")
    oid = await _place(maker, redis, qs, pid, now=OPEN)
    order = await _order(maker, oid)
    assert order.status == OrderStatus.filled
    assert D("100.00") <= order.avg_fill_price < D("100.50")


async def test_a_bracket_on_a_wide_book_is_referenced_on_the_last_trade(maker, redis, qs):
    """27/11 JPM: the placement read ask 338.63 (last ~323) and refused a bracket whose
    levels straddled the real price («take_profit > reference > stop_loss»)."""
    pid = await _portfolio(maker)
    qs.set_quote("AAPL", D("90.00"), D("110.00"), D("100.00"), provider="alpaca")
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
                qty=TEN,
            ), take_profit=D("105.00"), stop_loss=D("95.00"), now=OPEN,
        )
        await s.commit()
    assert entry.status != OrderStatus.rejected
    assert (tp.status, sl.status) != (OrderStatus.canceled, OrderStatus.canceled)
