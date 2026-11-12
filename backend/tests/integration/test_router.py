import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.provider import DataProvider, ProviderUnsupported
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.types import Bar, Capability, InstrumentHit, Quote, Timeframe


class _Fake(DataProvider):
    capabilities = frozenset(
        {Capability.quote, Capability.bars, Capability.search, Capability.stream}
    )
    asset_classes = frozenset({AssetClass.crypto})
    # behaviour, set per test: "ok" | "error" | "unsupported" | set of symbols to return
    mode: object = "ok"
    calls: ClassVar[list] = []

    async def get_quotes(self, items):
        type(self).calls.append(("quote", [s for s, _ in items]))
        mode = type(self).mode
        if mode == "error":
            raise RuntimeError("boom")
        if mode == "unsupported":
            raise ProviderUnsupported("nope")
        if mode == "slow":
            await asyncio.sleep(5)
        if mode == "config":
            return {s: Quote(s, None, None, Decimal(self.config.get("px", 0)), datetime.now(UTC),
                             self.name) for s, _ in items}
        wanted = mode if isinstance(mode, set) else {s for s, _ in items}
        return {
            s: Quote(s, Decimal(1), Decimal(2), Decimal("9" if ps == "X" else "1.5"),
                     datetime.now(UTC), self.name)
            for s, ps in items
            if s in wanted
        }

    async def get_bars(self, symbol, provider_symbol, timeframe, start, end):
        type(self).calls.append(("bars", symbol))
        if type(self).mode == "error":
            raise RuntimeError("boom")
        return [Bar(start, Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(10))]

    async def search(self, query, limit):
        type(self).calls.append(("search", query))
        return [
            InstrumentHit("BTC-USD", "Bitcoin", AssetClass.crypto, None, "USD", self.name),
            InstrumentHit(f"{self.name.upper()}-USD", "X", AssetClass.crypto, None, "USD",
                          self.name),
        ]

    def _default_symbol(self, symbol, asset_class):
        return symbol.replace("-", "")


class FakeA(_Fake):
    name = "fake_a"

    async def health_check(self):
        raise RuntimeError("down")


class FakeB(_Fake):
    name = "fake_b"

    async def health_check(self):
        return None


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    monkeypatch.setitem(PROVIDERS, "fake_a", FakeA)
    monkeypatch.setitem(PROVIDERS, "fake_b", FakeB)
    for cls in (FakeA, FakeB):
        monkeypatch.setattr(cls, "mode", "ok")
        monkeypatch.setattr(cls, "calls", [])
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add_all(
            [
                Connector(name="fake_a", enabled=True),
                Connector(name="fake_b", enabled=True),
                ProviderPriority(
                    asset_class=AssetClass.crypto,
                    capability=Capability.quote,
                    providers=["unknown_x", "fake_a", "fake_b"],
                ),
                ProviderPriority(
                    asset_class=AssetClass.crypto,
                    capability=Capability.bars,
                    providers=["fake_a", "fake_b"],
                ),
                ProviderPriority(
                    asset_class=AssetClass.crypto,
                    capability=Capability.stream,
                    providers=["fake_a", "fake_b"],
                ),
                ProviderPriority(
                    asset_class=AssetClass.crypto,
                    capability=Capability.search,
                    providers=["fake_a"],
                ),
                ProviderPriority(
                    asset_class=AssetClass.us_equity,
                    capability=Capability.search,
                    providers=["fake_b", "fake_a"],
                ),
            ]
        )
        for sym, override in (("BTC-USD", None), ("ETH-USD", {"fake_b": "X"})):
            s.add(
                Instrument(
                    symbol=sym,
                    name=sym,
                    asset_class=AssetClass.crypto,
                    market="crypto",
                    currency="USD",
                    provider_symbols=override or {},
                )
            )
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    async with httpx.AsyncClient() as http:
        yield MarketDataRouter(sm, redis, http), sm
    await redis.aclose()
    await engine.dispose()


async def _update_connector(sm, name, **fields):
    async with sm() as s:
        c = await s.get(Connector, name)
        for k, v in fields.items():
            setattr(c, k, v)
        await s.commit()


async def test_first_provider_used_and_cached(env):
    router, _ = env
    q = await router.get_quotes(["btc-usd"])
    assert q["BTC-USD"].provider == "fake_a"
    assert q["BTC-USD"].last == Decimal("1.5")
    again = await router.get_quotes(["BTC-USD"])
    assert again["BTC-USD"] == q["BTC-USD"]
    assert FakeA.calls == [("quote", ["BTC-USD"])]
    assert FakeB.calls == []

    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = await router.get_bars("BTC-USD", Timeframe.h1, start, start + timedelta(hours=3))
    bars2 = await router.get_bars("BTC-USD", Timeframe.h1, start, start + timedelta(hours=3))
    assert bars == bars2 and bars[0].close == Decimal("1.5")
    assert [c for c in FakeA.calls if c[0] == "bars"] == [("bars", "BTC-USD")]


async def test_fallback_on_error(env):
    router, _ = env
    FakeA.mode = "error"
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_b"
    assert FakeA.calls and FakeB.calls


async def test_disabled_provider_skipped(env):
    router, sm = env
    await _update_connector(sm, "fake_a", enabled=False)
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_b"
    assert FakeA.calls == []


async def test_rate_limited_provider_skipped(env):
    router, sm = env
    await _update_connector(sm, "fake_a", rate_limit_per_min=0)
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_b"
    assert FakeA.calls == []


async def test_all_fail_provider_unavailable(env):
    router, sm = env
    FakeA.mode = "error"
    await _update_connector(sm, "fake_b", rate_limit_per_min=0)
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["BTC-USD"])
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    errors = ei.value.details["errors"]
    assert set(errors) == {"fake_a", "fake_b"}
    assert errors["fake_a"] == "RuntimeError: boom"
    assert errors["fake_b"] == "rate_limited"

    with pytest.raises(SMTError) as ei:
        await router.get_bars(
            "BTC-USD", Timeframe.d1, datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE


async def test_all_unsupported_symbol_not_found(env):
    router, _ = env
    FakeA.mode = FakeB.mode = "unsupported"
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["BTC-USD"])
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND


async def test_unknown_instrument_symbol_not_found(env):
    router, _ = env
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["DOGE-USD"])
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert FakeA.calls == []


async def test_partial_quotes_merge(env):
    router, _ = env
    FakeA.mode = {"BTC-USD"}
    q = await router.get_quotes(["BTC-USD", "ETH-USD"])
    assert q["BTC-USD"].provider == "fake_a"
    assert q["ETH-USD"].provider == "fake_b"
    # provider_symbols override is passed to fake_b
    assert q["ETH-USD"].last == Decimal(9)
    assert FakeB.calls == [("quote", ["ETH-USD"])]


async def test_fx_same_currency_returns_one(env):
    router, _ = env
    assert await router.get_fx_rate("eur", "EUR") == Decimal(1)


async def test_stream_quote_in_redis_used(env):
    router, _ = env
    fresh = Quote("BTC-USD", Decimal(10), Decimal(11), Decimal("10.5"), datetime.now(UTC),
                  "stream")
    await write_quote(router._redis, fresh)
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"] == fresh
    assert FakeA.calls == [] and FakeB.calls == []


async def test_stale_stream_quote_refetched(env):
    router, _ = env
    old = Quote("BTC-USD", None, None, Decimal(10), datetime.now(UTC) - timedelta(seconds=30),
                "stream")
    await write_quote(router._redis, old)
    q = await router.get_quotes(["BTC-USD"], max_age_s=5)
    assert q["BTC-USD"].provider == "fake_a"


async def test_search_merges_classes_without_duplicates(env):
    router, _ = env
    hits = await router.search("bit")
    # us_equity (fake_b) first, then crypto (fake_a); BTC-USD kept once
    assert [(h.symbol, h.provider) for h in hits] == [
        ("BTC-USD", "fake_b"),
        ("FAKE_B-USD", "fake_b"),
        ("FAKE_A-USD", "fake_a"),
    ]
    assert len(await router.search("bit", limit=2)) == 2


async def test_provider_for_stream_and_health(env):
    router, sm = env
    await _update_connector(sm, "fake_a", enabled=False)
    p = await router.provider_for_stream(AssetClass.crypto)
    assert isinstance(p, FakeB)
    assert await router.provider_for_stream(AssetClass.forex) is None
    await _update_connector(sm, "fake_a", enabled=True)
    assert await router.check_health() == {"fake_a": False, "fake_b": True}
    async with sm() as s:
        a, b = await s.get(Connector, "fake_a"), await s.get(Connector, "fake_b")
        assert (a.health_status, a.last_error) == ("error", "RuntimeError: down")
        assert (b.health_status, b.last_error) == ("ok", None)


async def test_bad_args_invalid(env):
    router, _ = env
    with pytest.raises(SMTError) as ei:
        await router.get_bars(
            "BTC-USD", Timeframe.d1, datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert ei.value.code == ErrorCode.INVALID_ORDER


async def test_timeout_falls_back(env, monkeypatch):
    import smtrading.marketdata.router as router_mod

    router, _ = env
    monkeypatch.setattr(router_mod, "PROVIDER_TIMEOUT_S", 0.05)
    FakeA.mode = "slow"
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_b"


async def test_provider_instances_rebuilt_after_60s(env):
    from smtrading.connectors.service import update_connector

    router, sm = env
    now = [1000.0]
    router._clock = lambda: now[0]
    FakeA.mode = "config"
    assert (await router.get_quotes(["BTC-USD"], max_age_s=0))["BTC-USD"].last == 0
    async with sm() as s:
        await update_connector(s, "fake_a", config={"px": "7"})
        await s.commit()
    now[0] += 30
    assert (await router.get_quotes(["BTC-USD"], max_age_s=0))["BTC-USD"].last == 0
    now[0] += 31
    assert (await router.get_quotes(["BTC-USD"], max_age_s=0))["BTC-USD"].last == 7
