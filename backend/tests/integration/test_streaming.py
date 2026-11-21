import asyncio
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.cache import read_quote
from smtrading.marketdata.provider import DataProvider, StreamRefused
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.streaming import StreamManager, track, tracked_symbols, untrack
from smtrading.marketdata.types import Capability, Quote

FAST = 0.05


class FakeStream(DataProvider):
    name = "fake_stream"
    capabilities = frozenset({Capability.quote, Capability.stream})
    asset_classes = frozenset({AssetClass.crypto})
    # per test: number of initial connections that fail before yielding anything
    fail_first: ClassVar[int] = 0
    connections: ClassVar[list[list[tuple[str, str]]]] = []
    active: ClassVar[int] = 0
    max_active: ClassVar[int] = 0  # the most sockets ever open at once
    refuse_first: ClassVar[int] = 0  # initial connections refused with a «connection limit»
    polls: ClassVar[list[list[str]]] = []
    first_quote_delay: ClassVar[float] = 0

    def _default_symbol(self, symbol, asset_class):
        return symbol.replace("-", "")

    async def get_quotes(self, items):
        type(self).polls.append([s for s, _ in items])
        return {
            s: Quote(s, None, None, Decimal("1.5"), datetime.now(UTC), "poll") for s, _ in items
        }

    async def stream(self, items):
        cls = type(self)
        cls.connections.append(list(items))
        if len(cls.connections) <= cls.fail_first:
            raise ConnectionError("refused")
        if len(cls.connections) <= cls.refuse_first:
            raise StreamRefused("stream error 406: connection limit exceeded",
                                retry_after_s=0.4)
        cls.active += 1
        cls.max_active = max(cls.max_active, cls.active)
        try:
            await asyncio.sleep(cls.first_quote_delay)
            while True:
                for canonical, _ in items:
                    yield Quote(canonical, None, None, Decimal("2.5"), datetime.now(UTC),
                                self.name)
                await asyncio.sleep(FAST)
        finally:
            cls.active -= 1


class FakePoll(DataProvider):
    name = "fake_poll"
    capabilities = frozenset({Capability.quote})
    asset_classes = frozenset({AssetClass.us_equity})
    polls: ClassVar[list[list[str]]] = []

    def _default_symbol(self, symbol, asset_class):
        return symbol

    async def get_quotes(self, items):
        type(self).polls.append([s for s, _ in items])
        return {
            s: Quote(s, None, None, Decimal(190), datetime.now(UTC), self.name)
            for s, _ in items
        }


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "fake_stream", FakeStream)
    monkeypatch.setitem(PROVIDERS, "fake_poll", FakePoll)
    monkeypatch.setattr(FakeStream, "fail_first", 0)
    monkeypatch.setattr(FakeStream, "connections", [])
    monkeypatch.setattr(FakeStream, "active", 0)
    monkeypatch.setattr(FakeStream, "max_active", 0)
    monkeypatch.setattr(FakeStream, "refuse_first", 0)
    monkeypatch.setattr(FakeStream, "polls", [])
    monkeypatch.setattr(FakeStream, "first_quote_delay", 0)
    monkeypatch.setattr(FakePoll, "polls", [])
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add_all(
            [
                Connector(name="fake_stream", enabled=True),
                Connector(name="fake_poll", enabled=True),
                ProviderPriority(asset_class=AssetClass.crypto, capability=Capability.quote,
                                 providers=["fake_stream"]),
                ProviderPriority(asset_class=AssetClass.crypto, capability=Capability.stream,
                                 providers=["fake_stream"]),
                ProviderPriority(asset_class=AssetClass.us_equity, capability=Capability.quote,
                                 providers=["fake_poll"]),
                ProviderPriority(asset_class=AssetClass.us_equity,
                                 capability=Capability.stream, providers=[]),
                Instrument(symbol="AAPL", name="Apple", asset_class=AssetClass.us_equity,
                           market="us", currency="USD"),
                Instrument(symbol="BTC-USD", name="Bitcoin", asset_class=AssetClass.crypto,
                           market="crypto", currency="USD"),
                Instrument(symbol="ETH-USD", name="Ether", asset_class=AssetClass.crypto,
                           market="crypto", currency="USD",
                           provider_symbols={"fake_stream": "ETHX"}),
            ]
        )
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    async with httpx.AsyncClient() as http:
        yield MarketDataRouter(sm, redis, http), redis, sm
    await redis.aclose()
    await engine.dispose()


@pytest.fixture
async def manager(env):
    router, redis, sm = env
    mgr = StreamManager(router, redis, sm, reconcile_every_s=FAST, poll_every_s=FAST,
                        backoff_max_s=60)
    task = asyncio.create_task(mgr.run())
    try:
        yield mgr, redis
    finally:
        task.cancel()
        await asyncio.wait([task])
        assert task.cancelled()
        assert FakeStream.active == 0  # stream generators were closed


async def _until(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = pred()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_tracked_symbol_gets_streamed_quotes(manager):
    mgr, redis = manager
    await track(redis, ["btc-usd", "ETH-USD"], source="orders")

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed)
    assert (await read_quote(redis, "BTC-USD")).last == Decimal("2.5")
    # provider symbol override honoured when building stream items
    await _until(lambda: (mgr.status()["streams"].get("fake_stream:crypto")))
    assert ("ETH-USD", "ETHX") in FakeStream.connections[-1]
    assert ("BTC-USD", "BTCUSD") in FakeStream.connections[-1]
    status = mgr.status()
    assert status["streams"]["fake_stream:crypto"] == {
        "symbols": ["BTC-USD", "ETH-USD"], "state": "connected", "note": None,
    }
    assert status["owner"] is True
    assert status["polling"] == []
    published = json.loads(await redis.get("md:stream_status"))
    assert published["streams"]["fake_stream:crypto"]["symbols"] == ["BTC-USD", "ETH-USD"]
    assert 0 < await redis.ttl("md:stream_status") <= 30
    # once connected, the stream's symbols are no longer polled
    polls = len(FakeStream.polls)
    await asyncio.sleep(FAST * 4)
    assert len(FakeStream.polls) == polls


async def test_untrack_stops_stream(manager):
    mgr, redis = manager
    await track(redis, ["BTC-USD", "ETH-USD"], source="orders")
    await _until(lambda: (FakeStream.active == 1))
    await untrack(redis, ["ETH-USD"], source="orders")

    async def restarted():
        s = mgr.status()["streams"].get("fake_stream:crypto")
        return s is not None and s["symbols"] == ["BTC-USD"] and FakeStream.active == 1

    await _until(restarted)
    assert [s for s, _ in FakeStream.connections[-1]] == ["BTC-USD"]
    assert FakeStream.max_active == 1  # the old socket closed before the new one opened
    await untrack(redis, ["BTC-USD"], source="orders")
    await _until(lambda: (FakeStream.active == 0
                                and mgr.status()["streams"] == {}))
    assert mgr.status()["polling"] == []


async def test_stream_failure_falls_back_to_polling_and_reconnects(manager):
    mgr, redis = manager
    FakeStream.fail_first = 1
    await track(redis, ["BTC-USD"], source="positions")

    async def polled():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "poll"

    await _until(polled)
    assert mgr.status()["streams"]["fake_stream:crypto"]["state"] == "backoff"
    assert mgr.status()["polling"] == ["BTC-USD"]

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed, timeout=2.5)  # first backoff wait is 1s
    assert len(FakeStream.connections) == 2
    assert mgr.status()["streams"]["fake_stream:crypto"]["state"] == "connected"
    assert mgr.status()["polling"] == []


async def test_connection_limit_backs_off_longer_polls_and_notes_it(manager):
    """Alpaca's 406 «connection limit exceeded» (one socket per key: another client, or
    its own half-dead session): no 1-2-4 s hammering but the provider's longer wait,
    polled meanwhile, with a note in the published status."""
    mgr, redis = manager
    FakeStream.refuse_first = 1
    await track(redis, ["BTC-USD"], source="orders")

    async def polled():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "poll"

    await _until(polled)
    s = mgr.status()["streams"]["fake_stream:crypto"]
    assert s["state"] == "backoff" and "connection limit" in s["note"]
    async def published():
        body = json.loads(await redis.get("md:stream_status"))
        return "connection limit" in (body["streams"]["fake_stream:crypto"]["note"] or "")

    await _until(published)
    await asyncio.sleep(0.2)
    assert len(FakeStream.connections) == 1  # still waiting (0.4 s, not 1 s doubling)

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed, timeout=2)
    assert len(FakeStream.connections) == 2
    assert mgr.status()["streams"]["fake_stream:crypto"].get("note") is None


async def test_one_stream_owner_across_managers(env):
    """Two stream managers on the same Redis (a second worker, a deploy overlap): only
    the owner opens sockets; the other takes over when the owner stops."""
    router, redis, sm = env
    a = StreamManager(router, redis, sm, reconcile_every_s=FAST, poll_every_s=FAST,
                      owner_ttl_s=1)
    b = StreamManager(router, redis, sm, reconcile_every_s=FAST, poll_every_s=FAST,
                      owner_ttl_s=1)
    await track(redis, ["BTC-USD"], source="orders")
    ta = asyncio.create_task(a.run())
    await _until(lambda: FakeStream.active == 1)
    tb = asyncio.create_task(b.run())
    try:
        await asyncio.sleep(FAST * 6)
        assert FakeStream.max_active == 1 and len(FakeStream.connections) == 1
        assert b.status()["streams"] == {} and b.status()["owner"] is False
        ta.cancel()
        await asyncio.wait([ta])
        await _until(lambda: FakeStream.active == 1 and b.status()["owner"] is True,
                     timeout=3)
        assert FakeStream.max_active == 1
    finally:
        for t in (ta, tb):
            t.cancel()
        await asyncio.wait([ta, tb])
    assert FakeStream.active == 0


async def test_symbol_without_stream_provider_is_polled(manager):
    mgr, redis = manager
    await track(redis, ["AAPL"], source="watchlists")

    async def polled():
        q = await read_quote(redis, "AAPL")
        return q is not None and q.provider == "fake_poll"

    await _until(polled)
    await _until(lambda: (len(FakePoll.polls) >= 2))  # keeps polling
    assert mgr.status() == {"streams": {}, "polling": ["AAPL"], "owner": True}
    assert FakeStream.connections == []


async def test_multi_source_tracking(env):
    _, redis, _ = env
    await track(redis, ["BTC-USD"], source="orders")
    await track(redis, ["BTC-USD", "AAPL"], source="admin")
    assert await tracked_symbols(redis) == {"BTC-USD", "AAPL"}
    await untrack(redis, ["BTC-USD", "AAPL"], source="admin")
    assert await tracked_symbols(redis) == {"BTC-USD"}
    await untrack(redis, ["BTC-USD"], source="orders")
    assert await tracked_symbols(redis) == set()


async def test_unknown_source_is_rejected(env):
    _, redis, _ = env
    with pytest.raises(ValueError):
        await track(redis, ["BTC-USD"], source="nope")


async def test_stream_polled_until_first_quote(manager):
    mgr, redis = manager
    FakeStream.first_quote_delay = 0.5
    await track(redis, ["BTC-USD"], source="orders")

    async def connecting_and_polled():
        s = mgr.status()["streams"].get("fake_stream:crypto")
        return (s is not None and s["state"] == "connecting"
                and mgr.status()["polling"] == ["BTC-USD"] and FakeStream.polls)

    await _until(connecting_and_polled)
    await _until(lambda: (mgr.status()["streams"]["fake_stream:crypto"]["state"]
                          == "connected"), timeout=2)
    assert mgr.status()["polling"] == []
    assert len(FakeStream.connections) == 1


async def test_quote_write_error_does_not_drop_stream(manager, monkeypatch):
    import smtrading.marketdata.streaming as streaming_mod

    real = streaming_mod.write_quote
    failures = []

    async def flaky(redis, q, **kw):
        if not failures:
            failures.append(q.symbol)
            raise ConnectionError("redis blip")
        return await real(redis, q, **kw)

    monkeypatch.setattr(streaming_mod, "write_quote", flaky)
    mgr, redis = manager
    await track(redis, ["BTC-USD"], source="orders")

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed)
    assert failures == ["BTC-USD"]
    assert len(FakeStream.connections) == 1
    assert mgr.status()["streams"]["fake_stream:crypto"]["state"] == "connected"


async def test_stopped_stream_generator_closed_promptly(env, monkeypatch):
    import smtrading.marketdata.streaming as streaming_mod

    router, redis, sm = env
    blocked = asyncio.Event()

    async def stuck(redis, q, **kw):
        blocked.set()
        await asyncio.sleep(3600)  # cancelled in the loop body, not inside the generator

    monkeypatch.setattr(streaming_mod, "write_quote", stuck)
    # keep a reference to the generator: GC finalisation must not be what closes it
    gens = []
    real_stream = FakeStream.stream

    def keep(self, items):
        gens.append(real_stream(self, items))
        return gens[-1]

    monkeypatch.setattr(FakeStream, "stream", keep)
    mgr = StreamManager(router, redis, sm)
    await track(redis, ["BTC-USD"], source="orders")
    await mgr.reconcile()
    await asyncio.wait_for(blocked.wait(), 1)
    assert FakeStream.active == 1
    await mgr._stop(list(mgr._streams))
    assert FakeStream.active == 0  # aclosing ran the generator's finally right away


async def _set_market(sm, symbol, market):
    async with sm() as s:
        (await s.get(Instrument, symbol)).market = market
        await s.commit()


async def test_closed_market_polled_at_most_every_300s(env):
    from datetime import timedelta

    router, redis, sm = env
    await _set_market(sm, "AAPL", "NASDAQ")
    saturday = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
    now = [saturday]
    mgr = StreamManager(router, redis, sm, now=lambda: now[0])
    await track(redis, ["AAPL"], source="watchlists")
    await mgr.reconcile()
    await mgr.poll()
    assert FakePoll.polls == [["AAPL"]]
    now[0] += timedelta(seconds=10)
    await mgr.poll()
    assert FakePoll.polls == [["AAPL"]]  # closed: skipped
    now[0] += timedelta(seconds=291)
    await mgr.poll()
    assert FakePoll.polls == [["AAPL"]] * 2  # 301s later: refreshed once
    # open market (Monday 10:00 New York): every cycle
    now[0] = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
    await mgr.poll()
    now[0] += timedelta(seconds=5)
    await mgr.poll()
    assert len(FakePoll.polls) == 4


async def test_asset_class_groups_polled_concurrently(env, monkeypatch):
    router, redis, sm = env
    barrier = asyncio.Barrier(2)  # each group waits for the other: sequential would hang
    FakeStream.fail_first = 100  # keeps BTC-USD out of streaming (backoff -> polled)
    real_stream_q, real_poll_q = FakeStream.get_quotes, FakePoll.get_quotes

    async def stream_q(self, items):
        await asyncio.wait_for(barrier.wait(), 1)
        return await real_stream_q(self, items)

    async def poll_q(self, items):
        await asyncio.wait_for(barrier.wait(), 1)
        return await real_poll_q(self, items)

    monkeypatch.setattr(FakeStream, "get_quotes", stream_q)
    monkeypatch.setattr(FakePoll, "get_quotes", poll_q)
    mgr = StreamManager(router, redis, sm)
    await track(redis, ["AAPL", "BTC-USD"], source="orders")
    await mgr.reconcile()
    try:
        await _until(lambda: mgr.status()["streams"]["fake_stream:crypto"]["state"] == "backoff")
        await mgr.poll()
    finally:
        await mgr._stop(list(mgr._streams))
    assert FakePoll.polls == [["AAPL"]] and FakeStream.polls == [["BTC-USD"]]


class _ScriptedStream(DataProvider):
    """Connection i follows script[i]: "fail" raises at once, "quote" yields one then ends."""

    name = "scripted"

    def __init__(self, script):
        super().__init__({}, None)
        self.script = list(script)

    async def stream(self, items):
        step = self.script.pop(0)
        if step == "fail":
            raise ConnectionError("refused")
        yield Quote("BTC-USD", None, None, Decimal(1), datetime.now(UTC), self.name)


async def test_backoff_progression_caps_and_resets(monkeypatch):
    import smtrading.marketdata.streaming as streaming_mod
    from smtrading.marketdata.streaming import _Stream

    class _Stop(Exception):
        pass

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 8:
            raise _Stop

    async def fake_write(redis, q, **kw):
        return None

    monkeypatch.setattr(streaming_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(streaming_mod, "write_quote", fake_write)
    provider = _ScriptedStream(["fail"] * 5 + ["quote"] + ["fail"] * 2)
    mgr = StreamManager(None, None, None, backoff_max_s=8)
    stream = _Stream(provider, AssetClass.crypto, (("BTC-USD", "BTCUSD"),))
    with pytest.raises(_Stop):
        await mgr._run_stream("scripted:crypto", stream)
    # 1, 2, 4, 8, capped at 8; a delivered quote resets the delay to 1, then doubles again
    assert sleeps == [1, 2, 4, 8, 8, 1, 2, 4]
    assert stream.state == "backoff"
