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
from smtrading.marketdata.provider import DataProvider, StreamRefused, StreamSymbolLimit
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
    stream_symbol_limit: ClassVar[int | None] = None
    refuse_over: ClassVar[int | None] = None  # refuse («symbol limit») sets larger than this
    polls: ClassVar[list[list[str]]] = []
    first_quote_delay: ClassVar[float] = 0
    stream_kwargs: ClassVar[list[dict]] = []  # keyword arguments of each connection
    refuse_bars: ClassVar[bool] = False  # «symbol limit» whenever bars are asked

    def _default_symbol(self, symbol, asset_class):
        return symbol.replace("-", "")

    async def get_quotes(self, items):
        type(self).polls.append([s for s, _ in items])
        return {
            s: Quote(s, None, None, Decimal("1.5"), datetime.now(UTC), "poll") for s, _ in items
        }

    async def stream(self, items, **kw):
        cls = type(self)
        cls.connections.append(list(items))
        cls.stream_kwargs.append(kw)
        if cls.refuse_bars and kw.get("bars"):
            raise StreamSymbolLimit("stream error 405: symbol limit exceeded")
        if len(cls.connections) <= cls.fail_first:
            raise ConnectionError("refused")
        if len(cls.connections) <= cls.refuse_first:
            raise StreamRefused("stream error 406: connection limit exceeded",
                                retry_after_s=0.4)
        if cls.refuse_over is not None and len(items) > cls.refuse_over:
            raise StreamSymbolLimit("stream error 405: symbol limit exceeded")
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
    monkeypatch.setattr(FakeStream, "stream_symbol_limit", None)
    monkeypatch.setattr(FakeStream, "refuse_over", None)
    monkeypatch.setattr(FakeStream, "polls", [])
    monkeypatch.setattr(FakeStream, "first_quote_delay", 0)
    monkeypatch.setattr(FakeStream, "stream_kwargs", [])
    monkeypatch.setattr(FakeStream, "refuse_bars", False)
    monkeypatch.setattr(FakeStream, "supports_stream_bars", False, raising=False)
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


async def test_symbol_limit_streams_the_important_symbols_and_polls_the_rest(manager):
    """A provider with a symbol cap (Alpaca's free plan): symbols of orders, positions and
    alerts first, watchlist symbols after; the ones over the cap are polled."""
    _mgr, redis = manager
    FakeStream.stream_symbol_limit = 1
    await track(redis, ["ETH-USD"], source="watchlists")
    await track(redis, ["BTC-USD"], source="orders")

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    async def polled():
        q = await read_quote(redis, "ETH-USD")
        return q is not None and q.provider == "poll"

    await _until(streamed)
    await _until(polled)
    assert all(len(c) == 1 and c[0][0] == "BTC-USD" for c in FakeStream.connections)


async def test_symbol_limit_refusal_shrinks_the_set_and_keeps_polling(manager):
    """Alpaca's 405 «symbol limit exceeded»: the stream restarts with half the symbols
    (the important ones kept), the others are polled, no reconnect hammering."""
    mgr, redis = manager
    FakeStream.refuse_over = 1
    await track(redis, ["ETH-USD"], source="watchlists")
    await track(redis, ["BTC-USD"], source="orders")

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed, timeout=2)
    assert len(FakeStream.connections[0]) == 2  # first try: everything, refused
    assert [s for s, _ in FakeStream.connections[-1]] == ["BTC-USD"]
    assert len(FakeStream.connections) <= 3
    s = mgr.status()["streams"]["fake_stream:crypto"]
    assert "ETH-USD" not in s.get("symbols", ["ETH-USD"]) or s.get("note")


# ---- intraday mode B: reserved quota for rule symbols, 1-minute bars on the same socket


def _old_rank(sources, symbol):
    """The ranking before the auto_rules quota (copied verbatim from reconcile)."""
    return (0 if set(sources.get(symbol, [])) - {"watchlists"} else 1, symbol)


async def _add_crypto(sm, n: int) -> list[str]:
    syms = [f"C{i:02d}-USD" for i in range(n)]
    async with sm() as s:
        s.add_all([Instrument(symbol=sym, name=sym, asset_class=AssetClass.crypto,
                              market="crypto", currency="USD") for sym in syms])
        await s.commit()
    return syms


async def _reconciled(env, sources_by_symbol: dict[str, list[str]], cap: int = 30):
    """One reconcile with the given tracking: (manager, streamed symbols, polled symbols)."""
    router, redis, sm = env
    FakeStream.stream_symbol_limit = cap
    for sym, srcs in sources_by_symbol.items():
        for src in srcs:
            await track(redis, [sym], source=src)
    mgr = StreamManager(router, redis, sm)
    await mgr.reconcile()
    stream = mgr._streams.get("fake_stream:crypto")
    streamed = stream.symbols if stream else []
    polled = sorted(mgr._poll_only.get(AssetClass.crypto, []))
    return mgr, streamed, polled


async def test_no_auto_rules_same_subscription(env):
    """Review Focus 3: without auto_rules symbols the streamed set, the polled set and the
    call to the provider are the ones of today (old ranking copied above)."""
    _, redis, sm = env
    FakeStream.supports_stream_bars = True  # even a provider that could carry bars
    syms = await _add_crypto(sm, 33)  # + BTC-USD and ETH-USD: 35 symbols
    plan: dict[str, list[str]] = {}
    for i, sym in enumerate(syms):
        plan[sym] = (["orders"] if i % 3 == 0 else ["positions"] if i % 3 == 1
                     else ["watchlists"])
        if i % 5 == 0 and plan[sym] != ["watchlists"]:
            plan[sym] = [*plan[sym], "watchlists"]  # campaign symbols also on a list
    plan["BTC-USD"] = ["alerts", "watchlists"]
    plan["ETH-USD"] = ["watchlists"]
    mgr, streamed, polled = await _reconciled(env, plan)
    try:
        from smtrading.marketdata.streaming import tracked_sources

        sources = await tracked_sources(redis)
        assert "auto_rules" not in {s for v in sources.values() for s in v}
        ordered = sorted(plan, key=lambda s: _old_rank(sources, s))
        assert streamed == sorted(ordered[:30])
        assert polled == sorted(ordered[30:])
        await _until(lambda: FakeStream.connections)
        assert FakeStream.stream_kwargs == [{}]  # stream(items): no bars argument at all
        assert FakeStream.connections == [list(mgr._streams["fake_stream:crypto"].items)]
        assert "bars" not in mgr.status()["streams"]["fake_stream:crypto"]
        assert set(mgr.status()["streams"]["fake_stream:crypto"]) == {"symbols", "state",
                                                                     "note"}
    finally:
        await mgr._stop(list(mgr._streams))


async def test_reserve_never_displaces_campaign(env):
    """Review Focus 3: the quota of 10 takes only watchlist-only slots, never those of
    orders and positions."""
    syms = await _add_crypto(env[2], 45)
    # rule symbols sort first: a plain alphabetical tie-break would let them displace
    rules, campaign, lists = syms[:12], syms[12:37], syms[37:45]
    plan = {s: ["orders" if i % 2 else "positions"] for i, s in enumerate(campaign)}
    plan |= {s: ["watchlists"] for s in lists}
    plan |= {s: ["auto_rules"] for s in rules}
    mgr, streamed, polled = await _reconciled(env, plan)
    await mgr._stop(list(mgr._streams))
    assert set(campaign) <= set(streamed)
    assert set(streamed) - set(campaign) == set(sorted(rules)[:5])
    assert set(lists) <= set(polled) and set(sorted(rules)[5:]) <= set(polled)


async def test_reserve_never_displaces_campaign_full_cap(env):
    syms = await _add_crypto(env[2], 42)
    rules, campaign = syms[:12], syms[12:42]
    plan = {s: ["orders"] for s in campaign} | {s: ["auto_rules"] for s in rules}
    mgr, streamed, polled = await _reconciled(env, plan)
    await mgr._stop(list(mgr._streams))
    assert streamed == sorted(campaign)
    assert set(rules) <= set(polled)


async def test_reserve_takes_watchlist_slots(env):
    syms = await _add_crypto(env[2], 47)
    rules, orders, lists = syms[:12], syms[12:27], syms[27:47]
    plan = ({s: ["orders"] for s in orders} | {s: ["watchlists"] for s in lists}
            | {s: ["auto_rules"] for s in rules})
    mgr, streamed, polled = await _reconciled(env, plan)
    await mgr._stop(list(mgr._streams))
    assert len(streamed) == 30
    assert set(orders) <= set(streamed)
    assert set(streamed) & set(rules) == set(sorted(rules)[:10])
    assert set(streamed) & set(lists) == set(sorted(lists)[:5])
    assert set(polled) & set(rules) == set(sorted(rules)[10:])  # 2 rules over the quota
    assert len(polled) == 17


async def test_reserve_rule_symbol_on_a_watchlist_counts_as_rule(env):
    syms = await _add_crypto(env[2], 4)
    plan = {syms[0]: ["orders"], syms[1]: ["watchlists"], syms[2]: ["watchlists"],
            syms[3]: ["auto_rules", "watchlists"]}
    mgr, streamed, _ = await _reconciled(env, plan, cap=2)
    await mgr._stop(list(mgr._streams))
    assert streamed == sorted([syms[0], syms[3]])


async def test_bars_only_for_rule_symbols(env):
    FakeStream.supports_stream_bars = True
    syms = await _add_crypto(env[2], 5)
    plan = {syms[0]: ["orders"], syms[1]: ["orders", "auto_rules"], syms[2]: ["auto_rules"],
            syms[3]: ["auto_rules"], syms[4]: ["watchlists"]}
    mgr, streamed, polled = await _reconciled(env, plan, cap=3)
    try:
        assert streamed == sorted(syms[:3]) and polled == sorted(syms[3:])
        await _until(lambda: FakeStream.stream_kwargs)
        ps = {s: s.replace("-", "") for s in syms}
        assert FakeStream.stream_kwargs == [{"bars": frozenset({ps[syms[1]], ps[syms[2]]})}]
        assert mgr.status()["streams"]["fake_stream:crypto"]["bars"] == sorted(syms[1:3])
    finally:
        await mgr._stop(list(mgr._streams))


async def test_bars_not_asked_from_a_provider_without_bars(env):
    syms = await _add_crypto(env[2], 2)
    mgr, streamed, _ = await _reconciled(env, {syms[0]: ["auto_rules"],
                                               syms[1]: ["orders"]})
    try:
        await _until(lambda: FakeStream.stream_kwargs)
        assert streamed == sorted(syms) and FakeStream.stream_kwargs == [{}]
    finally:
        await mgr._stop(list(mgr._streams))


async def test_minute_bar_published(manager, monkeypatch):
    from smtrading.marketdata.minute_bars import StreamBar, read_minute_bars
    from smtrading.marketdata.types import Bar

    _mgr, redis = manager
    ts = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    bar = Bar(ts=ts, open=Decimal(1), high=Decimal(2), low=Decimal("0.5"),
              close=Decimal("1.5"), volume=Decimal(100))

    async def bars_only(self, items, **kw):
        FakeStream.connections.append(list(items))
        FakeStream.stream_kwargs.append(kw)
        FakeStream.active += 1
        try:
            for canonical, _ in items:
                yield StreamBar(canonical, bar)
            await asyncio.Event().wait()
        finally:
            FakeStream.active -= 1

    monkeypatch.setattr(FakeStream, "stream", bars_only)
    FakeStream.supports_stream_bars = True
    await track(redis, ["BTC-USD"], source="auto_rules")

    async def published():
        return await read_minute_bars(redis, "BTC-USD")

    [(_id, got)] = await _until(published)
    assert got == bar
    assert await redis.exists("quote:BTC-USD") == 0  # no write_quote, no quality guard
    assert FakeStream.stream_kwargs[0] == {"bars": frozenset({"BTCUSD"})}


async def test_bar_refusal_retries_without_bars(manager):
    """Review Focus 3: a «symbol limit» with bars asked never halves the campaign's
    symbols: the next socket carries the same items without bars, with a note."""
    mgr, redis = manager
    FakeStream.supports_stream_bars = True
    FakeStream.refuse_bars = True
    await track(redis, ["ETH-USD"], source="orders")
    await track(redis, ["BTC-USD"], source="auto_rules")

    async def streamed():
        q = await read_quote(redis, "BTC-USD")
        return q is not None and q.provider == "fake_stream"

    await _until(streamed, timeout=2)
    assert FakeStream.stream_kwargs[0] == {"bars": frozenset({"BTCUSD"})}
    assert FakeStream.stream_kwargs[-1] == {}
    assert FakeStream.connections[-1] == FakeStream.connections[0]  # same items
    assert len(FakeStream.connections) == 2
    assert mgr._caps == {}
    s = mgr.status()["streams"]["fake_stream:crypto"]
    assert s["note"] == ("Barre al minuto rifiutate dal fornitore: regole automatiche senza "
                         "volume e VWAP")
    assert s["symbols"] == ["BTC-USD", "ETH-USD"] and "bars" not in s
    await asyncio.sleep(FAST * 4)
    assert len(FakeStream.connections) == 2  # no retry loop with bars


async def test_socket_not_restarted_when_bars_unchanged(manager):
    mgr, redis = manager
    FakeStream.supports_stream_bars = True
    await track(redis, ["BTC-USD", "ETH-USD"], source="orders")
    await track(redis, ["BTC-USD"], source="auto_rules")
    await _until(lambda: (mgr.status()["streams"].get("fake_stream:crypto") or {}).get("state")
                 == "connected")
    await track(redis, ["BTC-USD", "ETH-USD"], source="watchlists")  # sources change only
    await asyncio.sleep(FAST * 5)
    assert len(FakeStream.connections) == 1 and FakeStream.max_active == 1
    await untrack(redis, ["BTC-USD"], source="auto_rules")  # the rule paused: bars change
    await _until(lambda: len(FakeStream.connections) == 2)
    assert FakeStream.connections[1] == FakeStream.connections[0]
    assert FakeStream.stream_kwargs == [{"bars": frozenset({"BTCUSD"})}, {}]
    await asyncio.sleep(FAST * 4)
    assert len(FakeStream.connections) == 2 and FakeStream.max_active == 1
