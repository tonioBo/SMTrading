"""BrokerSync on-demand wake-up (`brokersync:wake`), hot re-polls of automatic orders and
the heartbeat (`brokersync:last_pass`), with a fake idempotent (Alpaca-like) adapter."""

import asyncio
import itertools
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules.tables import AutoRule
from smtrading.brokers import registry
from smtrading.brokers import sync as sync_module
from smtrading.brokers.base import BrokerAdapter, BrokerOrder
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import AssetClass
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open


class Broker:
    """Shared by every adapter instance; an order fills `fill_after` s after its send."""

    def __init__(self) -> None:
        self.orders: dict[str, BrokerOrder] = {}
        self.placed: list[tuple[str, str, float]] = []  # (broker id, client ref, monotonic)
        self.placed_at: dict[str, float] = {}
        self.by_ref: dict[str, str] = {}
        self.gets = 0
        self.fill_after: float | None = None

    def calls(self) -> int:
        return len(self.placed) + self.gets


BROKER = Broker()


@registry.register
class FakeWakeAdapter(BrokerAdapter):
    kind: ClassVar[str] = "fake_wake_test"
    supported_types: ClassVar[frozenset[OrderType]] = frozenset({OrderType.market})
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(AssetClass)

    async def place_order(self, order, instrument) -> BrokerOrder:
        bid = f"w{len(BROKER.placed) + 1}"
        BROKER.placed.append((bid, self.client_ref(order), time.monotonic()))
        BROKER.placed_at[bid] = time.monotonic()
        BROKER.by_ref[self.client_ref(order)] = bid
        BROKER.orders[bid] = BrokerOrder(bid, "accepted", D(0), None, None)
        return BROKER.orders[bid]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        BROKER.gets += 1
        o = BROKER.orders[broker_order_id]
        if (BROKER.fill_after is not None
                and time.monotonic() - BROKER.placed_at[broker_order_id] >= BROKER.fill_after):
            o = BrokerOrder(broker_order_id, "filled", D(10), D("100"), None)
            BROKER.orders[broker_order_id] = o
        return o

    async def cancel_order(self, broker_order_id: str) -> None:
        o = BROKER.orders[broker_order_id]
        BROKER.orders[broker_order_id] = BrokerOrder(
            broker_order_id, "canceled", o.filled_qty, o.avg_fill_price, None)

    async def get_positions(self) -> dict[str, Decimal]:
        return {}

    async def find_order(self, order, instrument, since, exclude=frozenset()):
        bid = BROKER.by_ref.get(self.client_ref(order))
        return None if bid is None or bid in exclude else BROKER.orders[bid]


class Clock:
    def __call__(self) -> datetime:
        return OPEN


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    global BROKER
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    BROKER = Broker()
    yield


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
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


async def _portfolio(maker):
    async with maker() as s:
        p = await create_portfolio(s, name=f"W-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_wake_test", {})
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id


async def _place(maker, redis, qs, pid, *, auto=False):
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(10)), now=OPEN)
        if auto:  # a draft rule: no owner check, only the order's mark
            rule = AutoRule(portfolio_id=pid, symbol="AAPL", model="breakout")
            s.add(rule)
            await s.flush()
            await s.execute(update(Order).where(Order.id == order.id)
                            .values(auto_rule_id=rule.id))
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


def _sync(maker, redis, qs, interval_s):
    return BrokerSync(maker, redis, qs, None, clock=Clock(), interval_s=interval_s)


def _record_passes(sync) -> list[float]:
    """The monotonic start time of every full pass."""
    starts: list[float] = []
    tick = sync.tick

    async def recorded(**kw):
        starts.append(time.monotonic())
        await tick(**kw)

    sync.tick = recorded
    return starts


async def _wait_for(cond, timeout=5.0, step=0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await cond():
            return True
        await asyncio.sleep(step)
    return False


async def _subscribed(redis) -> bool:
    [(_, n)] = await redis.pubsub_numsub(sync_module.WAKE_CHANNEL)
    return n >= 1


async def _stop(task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _started(starts, redis, *, subscribed=True) -> None:
    """The first (recovery) pass ran and, if asked, the wake subscription is open."""
    assert await _wait_for(lambda: _true(bool(starts)))
    if subscribed:
        assert await _wait_for(lambda: _subscribed(redis), timeout=1.0)


async def _true(v: bool) -> bool:
    return v


async def test_wake_sends_within_300ms(maker, redis, qs):
    pid = await _portfolio(maker)
    sync = _sync(maker, redis, qs, interval_s=2)
    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())
    try:
        await _started(starts, redis)
        oid = await _place(maker, redis, qs, pid)
        t0 = time.monotonic()
        await sync_module.wake_broker_sync(redis, oid)
        assert await _wait_for(lambda: _true(bool(BROKER.placed)), timeout=1.0)
        [(_, ref, at)] = BROKER.placed
        assert ref == str(oid) and at - t0 < 0.3
        assert len(starts) == 1  # sent by the wake, not by a full pass

        async def linked():
            return (await _order(maker, oid)).broker_order_id == "w1"

        assert await _wait_for(linked, timeout=1.0)
    finally:
        await _stop(task)
    assert len(BROKER.placed) == 1


async def test_no_wake_same_cadence(maker, redis, qs):
    """Non-regression: without messages a full pass every interval_s and one broker
    call per order per pass (the send, then one read per pass), nothing else."""
    pid = await _portfolio(maker)
    await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs, interval_s=0.4)
    starts = _record_passes(sync)
    calls = []
    orig = sync.tick

    async def counted(**kw):
        await orig(**kw)
        calls.append(BROKER.calls())

    sync.tick = counted
    task = asyncio.create_task(sync.run())
    try:
        assert await _wait_for(lambda: _true(len(calls) >= 4), timeout=3.0)
    finally:
        await _stop(task)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert gaps and all(0.38 <= g <= 0.55 for g in gaps), gaps
    assert calls[:4] == [1, 2, 3, 4]  # send, then one read per pass
    assert len(BROKER.placed) == 1


async def test_wake_ignored_without_lock(maker, redis, qs, fresh_db):
    pid = await _portfolio(maker)
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        await conn.execute(text("select pg_advisory_lock(:k)"),
                           {"k": sync_module.ADVISORY_LOCK_KEY})
        await conn.commit()
        sync = _sync(maker, redis, qs, interval_s=0.2)
        task = asyncio.create_task(sync.run())
        try:
            await asyncio.sleep(0.3)
            oid = await _place(maker, redis, qs, pid)
            await sync_module.wake_broker_sync(redis, oid)
            await asyncio.sleep(0.5)
            assert BROKER.calls() == 0
            assert not await _subscribed(redis)  # a waiting worker does not listen
            assert await redis.get(sync_module.HEARTBEAT_KEY) is None
        finally:
            await _stop(task)
            await conn.execute(text("select pg_advisory_unlock(:k)"),
                               {"k": sync_module.ADVISORY_LOCK_KEY})
            await conn.commit()
    await engine.dispose()


async def test_wake_unknown_or_final_order_ignored(maker, redis, qs):
    pid = await _portfolio(maker)
    final = await _place(maker, redis, qs, pid)
    async with maker() as s:
        await s.execute(update(Order).where(Order.id == final)
                        .values(status=OrderStatus.canceled))
        await s.commit()
    sync = _sync(maker, redis, qs, interval_s=2)
    await sync.tick_orders([uuid.uuid4(), final])
    assert BROKER.calls() == 0
    assert (await _order(maker, final)).broker_order_id is None

    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())
    try:
        await _started(starts, redis)
        await redis.publish(sync_module.WAKE_CHANNEL, "not-a-uuid")
        await sync_module.wake_broker_sync(redis, uuid.uuid4())
        await sync_module.wake_broker_sync(redis, final)
        await asyncio.sleep(0.3)
        assert BROKER.calls() == 0 and not task.done()
    finally:
        await _stop(task)


async def test_hot_order_fill_seen_fast(maker, redis, qs):
    """An automatic order sent by a wake is read every HOT_POLL_S: a fill 0.6 s after the
    send is booked before 1.5 s; an order without auto_rule_id waits for the full pass."""
    pid = await _portfolio(maker)
    BROKER.fill_after = 0.6
    sync = _sync(maker, redis, qs, interval_s=3)
    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())
    try:
        await _started(starts, redis)
        auto = await _place(maker, redis, qs, pid, auto=True)
        plain = await _place(maker, redis, qs, pid)
        t0 = time.monotonic()
        await sync_module.wake_broker_sync(redis, auto)
        await sync_module.wake_broker_sync(redis, plain)

        async def auto_filled():
            return (await _order(maker, auto)).status == OrderStatus.filled

        assert await _wait_for(auto_filled, timeout=1.5)
        assert time.monotonic() - t0 < 1.5
        assert len(BROKER.placed) == 2
        p = await _order(maker, plain)
        assert p.broker_order_id is not None and p.status == OrderStatus.pending
        assert len(starts) == 1
    finally:
        await _stop(task)


async def test_hot_orders_at_most_hot_max(maker, redis, qs):
    pid = await _portfolio(maker)
    sync = _sync(maker, redis, qs, interval_s=3)
    ids = [await _place(maker, redis, qs, pid, auto=True)
           for _ in range(sync_module.HOT_MAX + 2)]
    await sync.tick_orders(ids)
    assert len(BROKER.placed) == sync_module.HOT_MAX + 2
    assert len(sync._hot) == sync_module.HOT_MAX


async def test_heartbeat_written_each_pass(maker, redis, qs):
    sync = _sync(maker, redis, qs, interval_s=0.3)
    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())
    try:
        async def beat():
            return await redis.get(sync_module.HEARTBEAT_KEY) is not None

        assert await _wait_for(beat, timeout=2.0)
        first = await redis.get(sync_module.HEARTBEAT_KEY)
        n = len(starts)
        assert await _wait_for(lambda: _true(len(starts) >= n + 2), timeout=2.0)
        await asyncio.sleep(0.05)
        second = await redis.get(sync_module.HEARTBEAT_KEY)
        assert second != first
        at = datetime.fromisoformat(second.decode())
        assert at.tzinfo is not None and abs((datetime.now(UTC) - at).total_seconds()) < 1
        assert 0 < await redis.ttl(sync_module.HEARTBEAT_KEY) <= 30
    finally:
        await _stop(task)


class _NoPubSub:
    """The real client, but its pub/sub fails (Redis unreachable for subscriptions)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.attempts = 0

    def pubsub(self):
        self.attempts += 1
        raise ConnectionError("redis down")

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_redis_down_falls_back_to_sleep(maker, redis, qs):
    broken = _NoPubSub(redis)
    sync = _sync(maker, broken, qs, interval_s=0.3)
    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())
    try:
        assert await _wait_for(lambda: _true(len(starts) >= 4), timeout=3.0)
    finally:
        await _stop(task)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(0.28 <= g <= 0.45 for g in gaps), gaps
    assert broken.attempts >= 1


async def test_burst_of_wakes_keeps_the_full_pass_cadence(maker, redis, qs):
    """Wakes without pause for an order at the broker: the full passes still run every
    interval_s and the order is sent once."""
    pid = await _portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs, interval_s=0.4)
    starts = _record_passes(sync)
    task = asyncio.create_task(sync.run())

    async def burst():
        while True:
            await sync_module.wake_broker_sync(redis, oid)
            await asyncio.sleep(0.002)

    flood = None
    try:
        await _started(starts, redis)
        flood = asyncio.create_task(burst())
        n = len(starts)
        assert await _wait_for(lambda: _true(len(starts) >= n + 3), timeout=3.0)
    finally:
        if flood is not None:
            await _stop(flood)
        await _stop(task)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(g <= 0.6 for g in gaps), gaps
    assert len(BROKER.placed) == 1 and BROKER.gets > len(starts)  # the wakes read it
