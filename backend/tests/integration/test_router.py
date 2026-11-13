import asyncio
from datetime import UTC, date, datetime, timedelta
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
from smtrading.marketdata.types import (
    Bar,
    CalendarEvent,
    Capability,
    InstrumentHit,
    NewsItem,
    Quote,
    ScreenFilter,
    Timeframe,
)


class _Fake(DataProvider):
    capabilities = frozenset(Capability)
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
        if mode == "delay":
            await asyncio.sleep(0.1)
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
        if type(self).mode == "forming":
            now = datetime.now(UTC)
            closed = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            return [
                Bar(ts, Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(10))
                for ts in (closed, closed + timedelta(hours=1))
            ]
        return [Bar(start, Decimal(1), Decimal(2), Decimal("0.5"), Decimal("1.5"), Decimal(10))]

    async def search(self, query, limit):
        type(self).calls.append(("search", query))
        return [
            InstrumentHit("BTC-USD", "Bitcoin", AssetClass.crypto, None, "USD", self.name),
            InstrumentHit(f"{self.name.upper()}-USD", "X", AssetClass.crypto, None, "USD",
                          self.name),
        ]

    async def get_news(self, symbols, limit):
        type(self).calls.append(("news", [s for s, _ in symbols]))
        hour = 11 if self.name == "fake_a" else 12
        return [
            NewsItem("shared", datetime(2026, 1, 1, 10, tzinfo=UTC), "s", None, None, "x", ()),
            NewsItem(f"{self.name}-1", datetime(2026, 1, 1, hour, tzinfo=UTC), "h", None, None,
                     "x", tuple(s for s, _ in symbols)),
        ]

    async def get_calendar(self, symbols, start, end):
        type(self).calls.append(("calendar", [s for s, _ in symbols]))
        day = 10 if self.name == "fake_a" else 1
        return [
            CalendarEvent(s, "earnings", date(2026, 2, day), {"eps": "1.2"}) for s, _ in symbols
        ]

    async def get_fx_rate(self, base, quote):
        type(self).calls.append(("fx", base, quote))
        return Decimal("1.0850")

    async def screen(self, f):
        type(self).calls.append(("screen", f.limit))
        return [
            {
                "symbol": "BTC-USD",
                "market_cap": Decimal("1.5E12"),
                "ts": datetime(2026, 1, 1, tzinfo=UTC),
                "day": date(2026, 1, 1),
                "nested": {"x": Decimal("0.1"), "l": [Decimal(2), "s", 3]},
                "n": 1,
            },
            {"symbol": "BTC-USD", "dup": True},
            {"symbol": "ETH-USD", "change_pct": Decimal("-1.25")},
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
                *(
                    ProviderPriority(asset_class=ac, capability=cap, providers=ps)
                    for ac, cap, ps in (
                        (AssetClass.crypto, Capability.news, ["fake_a"]),
                        (AssetClass.us_equity, Capability.news, ["fake_b"]),
                        (AssetClass.crypto, Capability.calendar, ["fake_a"]),
                        (AssetClass.us_equity, Capability.calendar, ["fake_b"]),
                        (AssetClass.forex, Capability.fx, ["fake_a"]),
                        (AssetClass.crypto, Capability.screener, ["fake_a"]),
                    )
                ),
                Instrument(
                    symbol="AAPL", name="Apple", asset_class=AssetClass.us_equity,
                    market="us", currency="USD",
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
    assert ei.value.details["asset_class"] == "crypto"
    assert ei.value.details["symbols"] == ["BTC-USD"]

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
    await write_quote(router._redis, old, fetched_at=old.ts)
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
    monkeypatch.setattr(router_mod, "QUOTE_TIMEOUT_PER_ITEM_S", 0.05)
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


async def test_bad_config_skipped_and_reported_by_health(env):
    router, sm = env
    await _update_connector(sm, "fake_a", config_encrypted=b"not-a-valid-ciphertext")
    q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_b"
    assert FakeA.calls == []
    assert await router.check_health() == {"fake_a": False, "fake_b": True}
    async with sm() as s:
        a = await s.get(Connector, "fake_a")
        assert a.health_status == "error" and a.last_error


async def test_constructor_error_recorded_and_falls_back(env, monkeypatch):
    router, _ = env

    def broken_init(self, config, http):
        raise ValueError("bad key")

    monkeypatch.setattr(FakeA, "__init__", broken_init)
    FakeB.mode = "error"
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["BTC-USD"])
    assert ei.value.details["errors"] == {
        "fake_a": "ValueError: bad key", "fake_b": "RuntimeError: boom",
    }
    FakeB.mode = "ok"
    assert (await router.get_quotes(["BTC-USD"]))["BTC-USD"].provider == "fake_b"


async def test_offhours_quote_fresh_by_fetched_at(env):
    router, _ = env
    old = Quote("BTC-USD", None, None, Decimal(10), datetime.now(UTC) - timedelta(hours=3),
                "stream")
    await write_quote(router._redis, old, fetched_at=datetime.now(UTC) - timedelta(seconds=2))
    assert (await router.get_quotes(["BTC-USD"]))["BTC-USD"] == old
    assert FakeA.calls == []
    # max_age_s=0 always forces a refresh
    assert (await router.get_quotes(["BTC-USD"], max_age_s=0))["BTC-USD"].provider == "fake_a"


async def test_concurrent_quotes_single_flight(env):
    router, _ = env
    FakeA.mode = "delay"
    results = await asyncio.gather(*(router.get_quotes(["BTC-USD"]) for _ in range(10)))
    assert len(FakeA.calls) == 1
    assert len({r["BTC-USD"] for r in results}) == 1


async def test_no_enabled_provider_reason(env):
    router, sm = env
    await _update_connector(sm, "fake_a", enabled=False)
    await _update_connector(sm, "fake_b", enabled=False)
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["BTC-USD"])
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert ei.value.details["reason"] == "no_enabled_provider"


async def test_screen_cache_roundtrip(env):
    router, _ = env
    f = ScreenFilter(asset_class=AssetClass.crypto, limit=5)
    first = await router.screen(f)
    second = await router.screen(f)
    assert first == second
    assert [r["symbol"] for r in second] == ["BTC-USD", "ETH-USD"]
    row = second[0]
    assert row["market_cap"] == Decimal("1.5E12") and isinstance(row["market_cap"], Decimal)
    assert row["ts"] == datetime(2026, 1, 1, tzinfo=UTC)
    assert row["day"] == date(2026, 1, 1)
    assert row["nested"] == {"x": Decimal("0.1"), "l": [Decimal(2), "s", 3]}
    assert second[1]["change_pct"] == Decimal("-1.25")
    assert [c for c in FakeA.calls if c[0] == "screen"] == [("screen", 5)]


async def test_news_merge_dedupe_sort_and_cache(env):
    router, _ = env
    first = await router.get_news(["BTC-USD", "AAPL"])
    second = await router.get_news(["AAPL", "BTC-USD"])
    assert [n.id for n in first] == ["fake_b-1", "fake_a-1", "shared"]
    assert second == first
    assert FakeA.calls == [("news", ["BTC-USD"])]
    assert FakeB.calls == [("news", ["AAPL"])]
    assert [n.id for n in await router.get_news(["BTC-USD", "AAPL"], limit=1)] == ["fake_b-1"]


async def test_calendar_sorted_and_cached(env):
    router, _ = env
    args = (["BTC-USD", "AAPL"], date(2026, 2, 1), date(2026, 3, 1))
    first = await router.get_calendar(*args)
    assert [(e.symbol, e.date) for e in first] == [
        ("AAPL", date(2026, 2, 1)), ("BTC-USD", date(2026, 2, 10)),
    ]
    assert await router.get_calendar(*args) == first
    assert len(FakeA.calls) == len(FakeB.calls) == 1


async def test_fx_cached(env):
    router, _ = env
    assert await router.get_fx_rate("eur", "usd") == Decimal("1.0850")
    assert await router.get_fx_rate("EUR", "USD") == Decimal("1.0850")
    assert FakeA.calls == [("fx", "EUR", "USD")]


async def test_forming_bar_dropped_and_cache_stores_filtered(env):
    router, _ = env
    FakeA.mode = "forming"
    end = datetime.now(UTC)
    start = end - timedelta(hours=5)
    bars = await router.get_bars("BTC-USD", Timeframe.h1, start, end)
    closed = end.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    assert [b.ts for b in bars] == [closed]
    assert await router.get_bars("BTC-USD", Timeframe.h1, start, end) == bars
    assert [c for c in FakeA.calls if c[0] == "bars"] == [("bars", "BTC-USD")]


# ---------------------------------------------------------------- final-review fixes


async def test_provider_answer_missing_symbols_recorded_as_error(env):
    router, _ = env
    FakeA.mode = {"BTC-USD"}  # answers, but without ETH-USD
    FakeB.mode = "error"
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["BTC-USD", "ETH-USD"])
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert ei.value.details["errors"] == {
        "fake_a": "missing: ETH-USD", "fake_b": "RuntimeError: boom",
    }
    assert ei.value.details["symbols"] == ["ETH-USD"]
    # next provider tried with only the missing symbol
    assert FakeB.calls == [("quote", ["ETH-USD"])]


async def test_total_yahoo_outage_is_provider_unavailable(env, monkeypatch):
    import smtrading.marketdata.providers.yahoo as yahoo_mod

    class DownYF:
        def fast_info(self, sym):
            raise RuntimeError("yahoo down")

    monkeypatch.setattr(yahoo_mod, "_YF", DownYF)
    router, sm = env
    async with sm() as s:
        s.add(Connector(name="yahoo", enabled=True))
        s.add(ProviderPriority(asset_class=AssetClass.us_equity, capability=Capability.quote,
                               providers=["yahoo"]))
        await s.commit()
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["AAPL"])
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert ei.value.details["errors"] == {"yahoo": "RuntimeError: yahoo down"}


async def test_batch_quote_costs_one_token_per_symbol(env):
    router, sm = env
    await _update_connector(sm, "fake_a", rate_limit_per_min=3)
    first = await router.get_quotes(["BTC-USD", "ETH-USD"], max_age_s=0)
    assert {q.provider for q in first.values()} == {"fake_a"}
    # 1 token left: a 2-symbol batch no longer fits and falls through to fake_b
    second = await router.get_quotes(["BTC-USD", "ETH-USD"], max_age_s=0)
    assert {q.provider for q in second.values()} == {"fake_b"}
    assert FakeA.calls == [("quote", ["BTC-USD", "ETH-USD"])]


async def test_quote_timeout_scales_with_batch(env, monkeypatch):
    import smtrading.marketdata.router as router_mod

    router, _ = env
    seen: list[float] = []
    real = asyncio.wait_for

    async def spy(aw, timeout):
        if getattr(aw, "__qualname__", "").startswith("_Fake."):  # provider calls only
            seen.append(timeout)
        return await real(aw, timeout)

    monkeypatch.setattr(router_mod.asyncio, "wait_for", spy)
    await router.get_quotes(["BTC-USD"], max_age_s=0)
    await router.get_quotes(["BTC-USD", "ETH-USD"], max_age_s=0)
    await router.get_fx_rate("EUR", "USD")
    assert seen == [10, 10, 10]
    monkeypatch.setattr(router_mod, "PROVIDER_TIMEOUT_S", 1)
    seen.clear()
    await router.get_quotes(["BTC-USD", "ETH-USD"], max_age_s=0)
    assert seen == [4]  # 2 symbols x QUOTE_TIMEOUT_PER_ITEM_S
    assert router_mod._batch_timeout(1) == 2 and router_mod._batch_timeout(7) == 14


async def test_rate_limiter_redis_error_fails_open(env, monkeypatch, caplog):
    import logging

    import smtrading.marketdata.router as router_mod

    async def broken(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(router_mod, "acquire", broken)
    router, _ = env
    with caplog.at_level(logging.WARNING, logger="smtrading.marketdata.router"):
        q = await router.get_quotes(["BTC-USD"])
    assert q["BTC-USD"].provider == "fake_a"
    assert any("rate limiter unavailable" in r.getMessage() for r in caplog.records)


async def test_calendar_skips_class_without_provider(env):
    from sqlalchemy import delete

    router, sm = env
    async with sm() as s:
        await s.execute(
            delete(ProviderPriority).where(
                ProviderPriority.asset_class == AssetClass.crypto,
                ProviderPriority.capability == Capability.calendar,
            )
        )
        await s.commit()
    events = await router.get_calendar(["AAPL", "BTC-USD"], date(2026, 2, 1), date(2026, 3, 1))
    assert [(e.symbol, e.date) for e in events] == [("AAPL", date(2026, 2, 1))]


async def _add_eurusd(sm):
    async with sm() as s:
        s.add(Instrument(symbol="EURUSD", name="EUR/USD", asset_class=AssetClass.forex,
                         market="FX", currency="USD"))
        await s.commit()


async def test_news_skips_class_without_provider(env):
    router, sm = env
    await _add_eurusd(sm)  # no forex news priority configured
    news = await router.get_news(["EURUSD", "AAPL"])
    assert [n.id for n in news] == ["fake_b-1", "shared"]
    assert FakeB.calls == [("news", ["AAPL"])]


async def test_news_real_failure_still_raises(env, monkeypatch):
    router, sm = env
    await _add_eurusd(sm)

    async def boom(self, symbols, limit):
        raise RuntimeError("news down")

    monkeypatch.setattr(FakeB, "get_news", boom)
    with pytest.raises(SMTError) as ei:
        await router.get_news(["EURUSD", "AAPL"])
    assert ei.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    assert ei.value.details["errors"] == {"fake_b": "RuntimeError: news down"}


async def test_single_class_without_provider_still_raises(env):
    router, sm = env
    await _add_eurusd(sm)  # no forex quote priority configured
    with pytest.raises(SMTError) as ei:
        await router.get_quotes(["EURUSD"])
    assert ei.value.details["reason"] == "no_enabled_provider"


async def test_bars_range_floored_so_close_requests_share_cache(env):
    router, _ = env
    start = datetime(2026, 1, 1, 10, 0, 7, tzinfo=UTC)
    end = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    a = await router.get_bars("BTC-USD", Timeframe.m1, start, end)
    b = await router.get_bars("BTC-USD", Timeframe.m1, start + timedelta(seconds=1),
                              end + timedelta(seconds=1))
    assert a == b and a[0].ts == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    # daily bars floor to the hour (min(timeframe, 1h))
    await router.get_bars("BTC-USD", Timeframe.d1, start, end)
    await router.get_bars("BTC-USD", Timeframe.d1, start + timedelta(minutes=30),
                          end + timedelta(minutes=30))
    assert [c for c in FakeA.calls if c[0] == "bars"] == [("bars", "BTC-USD")] * 2
    # a range inside one step cannot contain a bar start: no provider call
    assert await router.get_bars("BTC-USD", Timeframe.m1, start, start + timedelta(seconds=30)) == []
    assert len([c for c in FakeA.calls if c[0] == "bars"]) == 2


def test_router_import_registers_providers():
    import subprocess
    import sys

    code = (
        "import smtrading.marketdata.router\n"
        "from smtrading.marketdata.registry import PROVIDERS\n"
        "print(','.join(sorted(PROVIDERS)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "binance,coingecko,frankfurter,kraken,yahoo"


def test_describe_redacts_secrets():
    from smtrading.marketdata.router import _describe

    e = httpx.HTTPStatusError(
        "Client error '429' for url 'https://finnhub.io/api/v1/quote?symbol=A&token=abc'",
        request=httpx.Request("GET", "https://finnhub.io/api/v1/quote?token=abc"),
        response=httpx.Response(429),
    )
    out = _describe(e)
    assert "abc" not in out and "https://finnhub.io/api/v1/quote?<redacted>" in out
    assert "abc" not in _describe(RuntimeError("bad token=abc&x=1 and apikey=zzz key=q"))
    assert "zzz" not in _describe(RuntimeError("apikey=zzz")) and _describe(RuntimeError("ok")) == "RuntimeError: ok"
