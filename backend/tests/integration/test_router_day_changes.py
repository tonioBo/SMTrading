"""MarketDataRouter.get_day_changes: batch calls in chunks, one rate-limit cost per call,
per-symbol cache, fallback to the next quote provider, unknown symbols ignored."""

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
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.types import Capability, DayChange

D = Decimal


class _Batch(DataProvider):
    capabilities = frozenset({Capability.quote})
    asset_classes = frozenset({AssetClass.us_equity})
    day_change_chunk = 2
    calls: ClassVar[list] = []
    skip: ClassVar[set] = set()  # symbols it does not know
    fail: ClassVar[bool] = False

    def _default_symbol(self, symbol, asset_class):
        return symbol.lower()

    async def get_day_changes(self, items):
        type(self).calls.append([s for s, _ in items])
        if type(self).fail:
            raise RuntimeError("boom")
        return {
            s: DayChange(s, D("110"), D("100"), datetime.now(UTC), self.name)
            for s, ps in items if s not in type(self).skip and ps == s.lower()
        }


class BatchA(_Batch):
    name = "batch_a"


class BatchB(_Batch):
    name = "batch_b"
    day_change_chunk = 50


class NoBatch(DataProvider):
    """A quote provider without a batch day-change call: skipped."""

    name = "nobatch"
    capabilities = frozenset({Capability.quote})
    asset_classes = frozenset({AssetClass.us_equity})


SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE"]


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    for cls in (BatchA, BatchB):
        monkeypatch.setitem(PROVIDERS, cls.name, cls)
        monkeypatch.setattr(cls, "calls", [])
        monkeypatch.setattr(cls, "skip", set())
        monkeypatch.setattr(cls, "fail", False)
    monkeypatch.setitem(PROVIDERS, "nobatch", NoBatch)
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        (await s.get(Connector, "rssnews")).enabled = False
        s.add_all([
            Connector(name="nobatch", enabled=True),
            Connector(name="batch_a", enabled=True),
            Connector(name="batch_b", enabled=True),
            ProviderPriority(asset_class=AssetClass.us_equity, capability=Capability.quote,
                             providers=["nobatch", "batch_a", "batch_b"]),
            *(Instrument(symbol=sym, name=sym, asset_class=AssetClass.us_equity,
                         market="NYSE", currency="USD") for sym in SYMS),
            Instrument(symbol="OFF", name="off", asset_class=AssetClass.us_equity,
                       market="NYSE", currency="USD", active=False),
        ])
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    async with httpx.AsyncClient() as http:
        yield MarketDataRouter(sm, redis, http), sm, redis
    await redis.aclose()
    await engine.dispose()


async def test_chunks_cache_and_unknown_symbols(env):
    router, _, _ = env
    got = await router.get_day_changes(["aaa", *SYMS[1:], "NOPE", "OFF"])
    assert sorted(got) == SYMS
    assert got["AAA"].change_pct == D(10) and got["AAA"].provider == "batch_a"
    assert BatchA.calls == [["AAA", "BBB"], ["CCC", "DDD"], ["EEE"]]  # chunks of 2
    assert BatchB.calls == []
    again = await router.get_day_changes(SYMS)  # every symbol cached: no call
    assert again == got and len(BatchA.calls) == 3


async def test_missing_symbols_fall_back_to_the_next_provider(env):
    router, _, _ = env
    BatchA.skip = {"BBB", "EEE"}
    got = await router.get_day_changes(SYMS)
    assert {s: d.provider for s, d in got.items()} == {
        "AAA": "batch_a", "BBB": "batch_b", "CCC": "batch_a", "DDD": "batch_a",
        "EEE": "batch_b"}
    assert BatchB.calls == [["BBB", "EEE"]]  # one call for both


async def test_errors_never_raise_and_leave_symbols_out(env):
    router, _, _ = env
    BatchA.fail = BatchB.fail = True
    assert await router.get_day_changes(SYMS) == {}
    BatchA.fail = BatchB.fail = False
    assert sorted(await router.get_day_changes(SYMS)) == SYMS  # nothing cached on failure


async def test_rate_limit_costs_one_token_per_call(env):
    router, sm, redis = env
    async with sm() as s:  # batch_a: 2 tokens a minute -> two chunks, the rest to batch_b
        (await s.get(Connector, "batch_a")).rate_limit_per_min = 2
        (await s.get(Connector, "batch_b")).rate_limit_per_min = 1
        await s.commit()
    got = await router.get_day_changes(SYMS)
    assert BatchA.calls == [["AAA", "BBB"], ["CCC", "DDD"]]
    assert BatchB.calls == [["EEE"]]
    assert sorted(got) == SYMS
    await redis.delete(*[f"daychg:{s}" for s in SYMS])
    assert await router.get_day_changes(SYMS) == {}  # both buckets empty: nothing, no raise
    assert len(BatchA.calls) == 2 and len(BatchB.calls) == 1


async def test_empty_input(env):
    router, _, _ = env
    assert await router.get_day_changes([]) == {}
    assert await router.get_day_changes(["  "]) == {}
