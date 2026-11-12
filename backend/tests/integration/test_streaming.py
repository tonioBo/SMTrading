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
from smtrading.marketdata.provider import DataProvider
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
    polls: ClassVar[list[list[str]]] = []

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
        cls.active += 1
        try:
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
    monkeypatch.setattr(FakeStream, "polls", [])
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
        "symbols": ["BTC-USD", "ETH-USD"], "state": "connected",
    }
    assert status["polling"] == []
    published = json.loads(await redis.get("md:stream_status"))
    assert published["streams"]["fake_stream:crypto"]["symbols"] == ["BTC-USD", "ETH-USD"]
    assert FakeStream.polls == []


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


async def test_symbol_without_stream_provider_is_polled(manager):
    mgr, redis = manager
    await track(redis, ["AAPL"], source="watchlists")

    async def polled():
        q = await read_quote(redis, "AAPL")
        return q is not None and q.provider == "fake_poll"

    await _until(polled)
    await _until(lambda: (len(FakePoll.polls) >= 2))  # keeps polling
    assert mgr.status() == {"streams": {}, "polling": ["AAPL"]}
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
