"""The router's quote quality guard and provider quotas (plan 25), with fake providers."""

from datetime import UTC, datetime
from decimal import Decimal as D
from typing import ClassVar

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata import quality, quota
from smtrading.marketdata.cache import read_quote
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.types import Capability, Quote


class _P(DataProvider):
    """Answers `price` (a Decimal, an Exception to raise, or None for nothing)."""

    capabilities = frozenset({Capability.quote})
    asset_classes = frozenset({AssetClass.us_equity})
    price: ClassVar[object] = D(100)
    bid_ask: ClassVar[tuple | None] = None
    calls: ClassVar[list] = []

    def _default_symbol(self, symbol, asset_class):
        return symbol

    async def get_quotes(self, items):
        type(self).calls.append([s for s, _ in items])
        p = type(self).price
        if isinstance(p, Exception):
            raise p
        if p is None:
            return {}
        bid, ask = type(self).bid_ask or (None, None)
        return {s: Quote(s, bid, ask, p, datetime.now(UTC), self.name) for s, _ in items}


class QA(_P):
    name = "qa"


class QB(_P):
    name = "qb"


class QC(_P):
    name = "qc"


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    for cls in (QA, QB, QC):
        monkeypatch.setitem(PROVIDERS, cls.name, cls)
        monkeypatch.setattr(cls, "price", D(100))
        monkeypatch.setattr(cls, "bid_ask", None)
        monkeypatch.setattr(cls, "calls", [])
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        (await s.get(Connector, "rssnews")).enabled = False
        s.add_all([
            *(Connector(name=n, enabled=True) for n in ("qa", "qb", "qc")),
            Instrument(symbol="NVDA", name="NVIDIA", asset_class=AssetClass.us_equity,
                       market="us", currency="USD"),
        ])
        await s.merge(ProviderPriority(asset_class=AssetClass.us_equity,
                                       capability=Capability.quote, providers=["qa", "qb", "qc"]))
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    async with httpx.AsyncClient() as http:
        yield MarketDataRouter(sm, redis, http), redis, sm
    await redis.aclose()
    await engine.dispose()


async def _quote(router) -> Quote:
    return (await router.get_quotes(["NVDA"], max_age_s=0))["NVDA"]


async def _events(redis) -> dict:
    return await quality.read_events(redis, datetime.now(UTC).timestamp())


async def test_ok_quote_is_accepted_and_remembered(env):
    router, redis, _ = env
    q = await _quote(router)
    assert q.provider == "qa" and q.suspect is None
    assert QB.calls == []
    ctx = (await router.guard.contexts(["NVDA"]))["NVDA"]
    assert ctx.ref.price == D(100) and [o.provider for o in ctx.others] == ["qa"]
    assert await _events(redis) == {}


async def test_wide_spread_is_counted_only(env):
    router, redis, _ = env
    QA.bid_ask = (D("145.6"), D("178.29"))
    QA.price = D("176.5")
    q = await _quote(router)
    assert q.suspect is None and QB.calls == []
    assert await _events(redis) == {("qa", "NVDA", "wide_spread"): 1}


async def test_jump_confirmed_by_the_next_provider_is_accepted(env):
    router, _redis, _ = env
    await _quote(router)
    QA.price = QB.price = D(130)
    q = await _quote(router)
    assert q.provider == "qa" and q.suspect is None and q.last == D(130)
    assert QB.calls == [["NVDA"]] and QC.calls == []
    assert (await router.guard.contexts(["NVDA"]))["NVDA"].ref.price == D(130)


async def test_jump_of_one_provider_replaced_by_the_consensus(env):
    router, redis, _ = env
    await _quote(router)
    QA.price, QB.price = D(150), D("100.4")
    q = await _quote(router)
    assert q.provider == "qb" and q.suspect is None and q.last == D("100.4")
    assert (await read_quote(redis, "NVDA")).provider == "qb"
    assert await _events(redis) == {("qa", "NVDA", "jump"): 1}


async def test_unconfirmed_jump_is_passed_on_suspect(env):
    router, redis, _ = env
    await _quote(router)
    QA.price = D(150)
    QB.price = RuntimeError("down")
    QC.price = None
    q = await _quote(router)
    assert q.provider == "qa" and q.suspect == "jump" and q.last == D(150)
    cached = await read_quote(redis, "NVDA")
    assert cached.suspect == "jump"
    now = datetime.now(UTC).timestamp()
    assert [s[1:] for s in await quality.read_suspects(redis, now)] == [("qa", "NVDA", "jump")]
    assert "NVDA" in await quality.suspect_since(redis, ["NVDA"])
    # the reference is not moved by a suspect quote; the next good one clears the run
    assert (await router.guard.contexts(["NVDA"]))["NVDA"].ref.price == D(100)
    QA.price = D("100.2")
    q = await _quote(router)
    assert q.suspect is None and (await read_quote(redis, "NVDA")).suspect is None
    assert await quality.suspect_since(redis, ["NVDA"]) == {}


async def test_disagreeing_providers_walk_to_a_third_for_consensus(env):
    router, _redis, _ = env
    await _quote(router)  # reference 100 from qa
    # qb answered 2 minutes ago at 100 (cached); qa now says 104: within the jump but not
    # with qb → disagree; qb (now 104.5) confirms qa
    await router.guard.accepted([Quote("NVDA", None, None, D(100), datetime.now(UTC), "qb")])
    QA.price, QB.price = D(104), D("104.5")
    q = await _quote(router)
    assert q.provider == "qa" and q.suspect is None and QB.calls == [["NVDA"]]


async def test_disagree_without_consensus_is_suspect(env):
    router, _redis, _ = env
    # each answer disagrees with another provider's recent price, and no two agree
    await router.guard.accepted([Quote("NVDA", None, None, D(100), datetime.now(UTC), p)
                                 for p in ("qb", "qc")])
    QA.price, QB.price, QC.price = D(104), D(108), D(112)
    q = await _quote(router)
    assert q.provider == "qa" and q.suspect == "disagree"
    assert QC.calls == [["NVDA"]]


async def test_stream_tick_check(env):
    router, _redis, _ = env
    await _quote(router)
    tick = Quote("NVDA", None, None, D(150), datetime.now(UTC), "qa")
    marked = await router.guard.check_stream(tick, AssetClass.us_equity)
    assert marked.suspect == "jump"
    ok = await router.guard.check_stream(
        Quote("NVDA", None, None, D(101), datetime.now(UTC), "qa"), AssetClass.us_equity)
    assert ok.suspect is None


# -- quotas -----------------------------------------------------------------------------

async def _set_budget(sm, name, **changes):
    async with sm() as s:
        await quota.save_override(s, name, changes)
        await s.commit()


async def test_every_admitted_call_is_counted(env):
    router, redis, _ = env
    for _ in range(3):
        await _quote(router)
    used = (await quota.usage(redis, ["qa", "qb"]))
    assert (used["qa"].minute, used["qa"].hour, used["qa"].day) == (3, 3, 3)
    assert used["qb"].day == 0


async def test_provider_near_its_daily_budget_is_demoted(env):
    router, _redis, sm = env
    await _set_budget(sm, "qa", per_day=10)
    for _ in range(8):
        await _quote(router)
    assert QB.calls == []
    await _quote(router)  # the 9th call: 90% used
    q = await _quote(router)
    assert q.provider == "qb"
    # still used when the others do not answer
    QB.price = QC.price = RuntimeError("down")
    assert (await _quote(router)).provider == "qa"


async def test_429_demotes_at_once_until_reset(env):
    router, redis, sm = env
    await _set_budget(sm, "qa", per_day=800)
    QA.price = RuntimeError("twelvedata: HTTP 429")
    q = await _quote(router)
    assert q.provider == "qb"
    QA.price = D(100)
    QA.calls.clear()
    assert (await _quote(router)).provider == "qb" and QA.calls == []
    used = (await quota.usage(redis, ["qa"]))["qa"]
    assert used.blocked_until == quota.next_reset(quota.DAY, datetime.now(UTC))
    await quota.unblock(redis, "qa")  # the reset (or the admin)
    assert (await _quote(router)).provider == "qa"


async def test_429_without_daily_budget_blocks_for_the_minute(env):
    router, redis, _ = env
    QA.price = RuntimeError("HTTP 429 Too Many Requests")
    await _quote(router)
    used = (await quota.usage(redis, ["qa"]))["qa"]
    assert used.blocked_until == quota.next_reset(quota.MINUTE, datetime.now(UTC)) \
        or (used.blocked_until - datetime.now(UTC)).total_seconds() <= 61


async def test_only_provider_is_never_skipped(env):
    router, redis, sm = env
    async with sm() as s:
        row = await s.get(ProviderPriority, (AssetClass.us_equity, Capability.quote))
        row.providers = ["qa"]
        await s.commit()
    await quota.block(redis, "qa", quota.Budget(60, None, 800))
    assert (await _quote(router)).provider == "qa"
