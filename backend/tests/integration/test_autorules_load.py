"""Load test of the `scalper` (intraday mode B): 30 active rules (30 symbols, mixed
models) at 20 quotes/s each through `write_quote` (the stream's own path) and a simulated
minute bar every 5 s through `publish_minute_bar`. The scalper keeps up, the existing
exits (OrderProcessor) are not delayed and the stream manager publishes the campaign's
quotes as fast as without rules.

Real clock and the test Redis; made-up CRYPTO symbols (always open, whatever the day the
test runs). Every bound is relative or generous: a loaded machine slows both sides of a
comparison. The scalper of the exit and stream tests runs in its own process, as in
production (the `scalper` service): only Redis and Postgres are shared with the worker's
loops under test; in the exit test the child also writes the price flow, so the loop
under test runs only the OrderProcessor and its probe."""
import asyncio
import contextlib
import json
import math
import os
import statistics
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import ClassVar

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules import rules
from smtrading.autorules.service import Scalper
from smtrading.brokers.sync import HEARTBEAT_KEY
from smtrading.common.db import make_engine
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.cache import read_quote, write_quote
from smtrading.marketdata.minute_bars import StreamBar, publish_minute_bar
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.serialization import loads_quote
from smtrading.marketdata.streaming import STATUS_KEY, StreamManager, track
from smtrading.marketdata.types import Bar, Capability, Quote
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

BY = "admin@example.com"
N_RULES = 30
RATE_HZ = 20  # quotes per second per rule symbol
LOAD_S = 20
BAR_EVERY_S = 5
WARMUP_BARS = 60
MODEL_PARAMS = (  # mixed models; no cooldown so every trigger reaches the executor
    ("breakout", {"lookback_min": 5, "cooldown_s": 0}),
    ("mean_reversion", {"lookback_min": 10, "z": D(1), "cooldown_s": 0}),
    ("vwap_pullback", {"min_above_min": 3, "pullback_pct": D(1), "cooldown_s": 0}),
)
RULE_SYMBOLS = [f"LR{i:02d}-USD" for i in range(N_RULES)]
EXIT_SYMBOL = "LX00-USD"  # the resting stop of a portfolio without rules
SAMPLES = 15  # exits measured per phase
CAMPAIGN_SYMBOLS = [f"LC{i:02d}-USD" for i in range(10)]


def _p(sorted_values: list[float], pct: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, math.ceil(pct * len(sorted_values)) - 1)]


def _price(i: int, k: int) -> D:
    """Around 100 ± 0.3: crosses the bars' highs, means and VWAP now and then."""
    return D(100) + D(round(0.3 * math.sin(k / 7 + i), 4))


def _quote(symbol: str, price: D, ts: datetime | None = None) -> Quote:
    return Quote(symbol=symbol, bid=price - D("0.01"), ask=price + D("0.01"), last=price,
                 ts=ts or datetime.now(UTC), provider="load")


def _bar(i: int, ts: datetime) -> Bar:
    c = D(100) + D(round(0.2 * math.sin(ts.minute + i), 4))
    return Bar(ts=ts, open=c, high=c + D("0.1"), low=c - D("0.1"), close=c,
               volume=D(1000))


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        s.add_all([Instrument(symbol=sym, name=sym, asset_class=AssetClass.crypto,
                              market="CRYPTO", currency="USD")
                   for sym in [*RULE_SYMBOLS, EXIT_SYMBOL, *CAMPAIGN_SYMBOLS]])
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def _rules(sm) -> None:
    """30 active rules on three portfolios, one per symbol, the models in turn."""
    async with sm() as s:
        portfolios = []
        for _ in range(3):
            p = await create_portfolio(s, name=f"L-{uuid.uuid4().hex[:8]}",
                                       initial_cash=D(100000), base_currency="USD")
            p.auto_rules_enabled = True
            portfolios.append(p)
        await s.flush()
        now = datetime.now(UTC)
        for i, sym in enumerate(RULE_SYMBOLS):
            model, params = MODEL_PARAMS[i % len(MODEL_PARAMS)]
            r = await rules.create_rule(s, portfolio_id=portfolios[i % 3].id, symbol=sym,
                                        model=model, params=params, qty=D(1), by=BY)
            await rules.activate(s, r.id, by=BY, now=now)
        await s.commit()


async def _warmup_bars(redis) -> datetime:
    """The last hour of minute bars of every rule symbol on `bars1m:` (the warm-up reads
    them); returns the next minute to publish."""
    first = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=WARMUP_BARS)
    for i, sym in enumerate(RULE_SYMBOLS):
        for m in range(WARMUP_BARS):
            await publish_minute_bar(redis, StreamBar(sym, _bar(i, first + timedelta(minutes=m))))
    return first + timedelta(minutes=WARMUP_BARS)


async def _stream_status(redis) -> None:
    """The rule symbols on a connected stream (the stream guard passes; the entries stop
    at the BrokerSync guard: no heartbeat, nothing reaches the database)."""
    await redis.set(STATUS_KEY, json.dumps({"streams": {"load:crypto": {
        "symbols": RULE_SYMBOLS, "state": "connected", "note": None,
        "bars": RULE_SYMBOLS}}, "polling": []}), ex=120)
    await redis.delete(HEARTBEAT_KEY)


class Load:
    """RATE_HZ quotes/s for every rule symbol through `write_quote`, a minute bar for each
    every BAR_EVERY_S through `publish_minute_bar`, on an absolute schedule."""

    def __init__(self, redis, next_bar: datetime) -> None:
        self._redis = redis
        self._next_bar = next_bar
        self.written = 0
        self.bars = 0

    async def run(self, duration_s: float | None) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        k = 0
        next_bar_at = start + BAR_EVERY_S
        while duration_s is None or loop.time() - start < duration_s:
            await asyncio.gather(*(write_quote(self._redis, _quote(sym, _price(i, k)))
                                   for i, sym in enumerate(RULE_SYMBOLS)))
            self.written += N_RULES
            if loop.time() >= next_bar_at:
                for i, sym in enumerate(RULE_SYMBOLS):
                    await publish_minute_bar(self._redis, StreamBar(sym, _bar(i, self._next_bar)))
                self._next_bar += timedelta(minutes=1)
                self.bars += 1
                next_bar_at += BAR_EVERY_S
            k += 1
            await asyncio.sleep(max(0.0, start + k / RATE_HZ - loop.time()))


class NoHistory:
    """The router of the warm-up: no REST history, the `bars1m:` entries are used."""

    async def get_bars(self, *a, **k):
        return []


# --- the scalper in its own process (as the `scalper` service) ----------------------

SCALPER_PROCESS = r"""
import asyncio, os, sys
from datetime import datetime
sys.path.insert(0, os.environ["LOADTEST_DIR"])
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker
import smtrading.models  # noqa: F401
from smtrading.autorules.service import Scalper
from smtrading.common.db import make_engine
from test_autorules_load import Load, NoHistory

async def main():
    engine = make_engine(os.environ["LOADTEST_DB_URL"])
    redis = aioredis.from_url(os.environ["LOADTEST_REDIS_URL"])
    scalper = Scalper(async_sessionmaker(engine, expire_on_commit=False), redis, NoHistory())
    await scalper.reload()
    tasks = [scalper.run()]
    if next_bar := os.environ.get("LOADTEST_NEXT_BAR"):  # the price flow, from here too
        tasks.append(Load(redis, datetime.fromisoformat(next_bar)).run(None))
    await redis.set("loadtest:scalper_ready", sum(
        len(scalper.runners(s)) for s in scalper._by_symbol))
    await asyncio.gather(*tasks)

asyncio.run(main())
"""


@contextlib.asynccontextmanager
async def scalper_process(db_url: str, redis, redis_url: str, *,
                          load_from: datetime | None = None):
    """The scalper in a child process (as the `scalper` service); with `load_from` the
    child also runs the price flow (`Load`, bars from that minute), so the loop under test
    carries none of it. Its stderr goes to a file (never a pipe that could fill up)."""
    await redis.delete("loadtest:scalper_ready")
    env = {**os.environ, "LOADTEST_DIR": os.path.dirname(os.path.abspath(__file__)),
           "LOADTEST_DB_URL": db_url, "LOADTEST_REDIS_URL": redis_url}
    if load_from is not None:
        env["LOADTEST_NEXT_BAR"] = load_from.isoformat()
    with tempfile.TemporaryFile() as err:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", SCALPER_PROCESS, stdout=asyncio.subprocess.DEVNULL,
            stderr=err, env=env)

        def stderr_tail() -> str:
            err.seek(0)
            return err.read().decode(errors="replace")[-2000:]

        try:
            deadline = time.monotonic() + 60
            while (ready := await redis.get("loadtest:scalper_ready")) is None:
                if proc.returncode is not None or time.monotonic() > deadline:
                    await _stop(proc)
                    raise AssertionError(f"scalper process not ready: {stderr_tail()}")
                await asyncio.sleep(0.1)
            assert int(ready) == N_RULES
            yield proc
            assert proc.returncode is None, f"the scalper process died: {stderr_tail()}"
        finally:
            await _stop(proc)


async def _stop(proc) -> None:
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()


# --- 1. the scalper keeps up ---------------------------------------------------------

async def test_scalper_keeps_up_with_30_symbols(maker, redis):
    await _rules(maker)
    next_bar = await _warmup_bars(redis)
    await _stream_status(redis)
    scalper = Scalper(maker, redis, NoHistory())
    await scalper.reload()
    assert sum(len(scalper.runners(s)) for s in RULE_SYMBOLS) == N_RULES

    durations: list[float] = []
    evaluated: Counter[str] = Counter()
    ages: list[float] = []
    on_event = scalper.on_event

    async def timed(ev):
        t0 = time.perf_counter()
        await on_event(ev)
        if isinstance(ev, Quote):
            durations.append(time.perf_counter() - t0)
            evaluated[ev.symbol] += 1
            ages.append((datetime.now(UTC) - ev.ts).total_seconds())

    scalper.on_event = timed
    load = Load(redis, next_bar)
    task = asyncio.create_task(scalper.run())
    try:
        await asyncio.sleep(0.5)  # the reader subscribed
        await load.run(LOAD_S)
        await asyncio.sleep(0.5)  # the last quotes consumed
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    rate = {sym: evaluated[sym] / LOAD_S for sym in RULE_SYMBOLS}
    slowest = min(rate.values())
    assert slowest >= 15, f"a symbol evaluated {slowest:.1f}/s (written {RATE_HZ}/s)"
    durations.sort()
    p99 = _p(durations, 0.99)
    assert p99 < 0.010, f"on_event p99 {p99 * 1000:.2f} ms"
    ages.sort()
    # the quotes reach the rules fresh: well within the price-age guard (2 s)
    assert _p(ages, 0.99) < 0.5, f"quote age at evaluation p99 {_p(ages, 0.99):.3f} s"
    # the entries went through the executor's guards (blocked: no BrokerSync heartbeat)
    blocked = 0
    async for key in redis.scan_iter("autorules:blocked:*"):
        blocked += sum(int(v) for v in (await redis.hgetall(key)).values())
    assert blocked > 0
    # bounded memory: the runners keep only their series, whatever the bars received
    assert load.bars >= LOAD_S // BAR_EVERY_S - 1
    for sym in RULE_SYMBOLS:
        [(_, runner)] = scalper.runners(sym)
        assert runner.series.count() == runner.series._bars.maxlen == runner.warmup + 5
    print(f"\nscalper: {load.written} quotes, slowest {slowest:.1f}/s, on_event p99 "
          f"{p99 * 1000:.2f} ms, age p99 {_p(ages, 0.99) * 1000:.0f} ms, {blocked} entries")


# --- 2. the existing exits are not delayed ------------------------------------------

async def _exit_latencies(sm, redis, pid, qs, n: int, throttle_s: float) -> list[float]:
    """`n` times: a resting sell stop, the price back at 100, then a quote through the
    stop: the time from that quote's write to the order leaving `pending`."""
    out = []
    for _ in range(n):
        async with sm() as s:
            order = await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol=EXIT_SYMBOL, side=OrderSide.sell,
                type=OrderType.stop, qty=D(1), stop_price=D(95)), now=datetime.now(UTC))
            await s.commit()
            await dispatch_post_commit(s, redis)
            oid = order.id
        await write_quote(redis, _quote(EXIT_SYMBOL, D(100)))
        await asyncio.sleep(2 * throttle_s)  # past the processor's per-symbol throttle
        t0 = time.perf_counter()
        await write_quote(redis, _quote(EXIT_SYMBOL, D(90)))
        while True:
            async with sm() as s:
                status = await s.scalar(select(Order.status).where(Order.id == oid))
            if status != OrderStatus.pending:
                out.append(time.perf_counter() - t0)
                assert status in (OrderStatus.triggered, OrderStatus.filled)
                break
            if time.perf_counter() - t0 > 5:
                raise AssertionError("the stop never triggered")
            await asyncio.sleep(0.005)
    return out


async def test_existing_exits_not_delayed(maker, redis, redis_url, fresh_db):
    async with maker() as s:  # no rules, nothing on the 30 symbols
        p = await create_portfolio(s, name="Uscite", initial_cash=D(1000000),
                                   base_currency="USD")
        await s.commit()
        pid = p.id
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote(EXIT_SYMBOL, D("99.99"), D("100.01"), D(100))
    async with maker() as s:
        await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol=EXIT_SYMBOL, side=OrderSide.buy, type=OrderType.market,
            qty=D(100)), now=datetime.now(UTC))
        await place_order(s, qs, PlaceOrderRequest(  # keeps the symbol followed
            portfolio_id=pid, symbol=EXIT_SYMBOL, side=OrderSide.sell, type=OrderType.stop,
            qty=D(1), stop_price=D(1)), now=datetime.now(UTC))
        await s.commit()
    proc = OrderProcessor(maker, redis, qs)
    throttle = proc._min_eval_interval_s
    processor = asyncio.create_task(proc.run())
    try:
        await asyncio.sleep(proc._sweep_every_s * 1.5)  # a sweep: the symbol is active
        idle = await _exit_latencies(maker, redis, pid, qs, SAMPLES, throttle)

        await _rules(maker)
        next_bar = await _warmup_bars(redis)
        await _stream_status(redis)
        # the price flow from the child too: this loop runs only the processor and probe
        async with scalper_process(fresh_db, redis, redis_url, load_from=next_bar):
            await asyncio.sleep(1)  # under load
            loaded = await _exit_latencies(maker, redis, pid, qs, SAMPLES, throttle)
            for sym in RULE_SYMBOLS:  # the flow was on all along
                age = datetime.now(UTC) - (await read_quote(redis, sym)).ts
                assert age < timedelta(seconds=1), (sym, age)
        assert not processor.done()
    finally:
        processor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await processor

    idle.sort()
    loaded.sort()
    diff = statistics.median(loaded) - statistics.median(idle)
    assert diff < 0.050, f"median exit {diff * 1000:.0f} ms slower under load"
    p95 = _p(loaded, 0.95)
    assert p95 < throttle + 0.25, f"exit p95 under load {p95:.3f} s"
    print(f"\nexits: idle median {statistics.median(idle) * 1000:.1f} ms, loaded median "
          f"{statistics.median(loaded) * 1000:.1f} ms, loaded p95 {p95 * 1000:.1f} ms")


# --- 3. the stream manager is unaffected ----------------------------------------------

class LoadStream(DataProvider):
    """A stream of every symbol asked at RATE_HZ, each quote stamped when it is made."""

    name = "load_stream"
    capabilities = frozenset({Capability.quote, Capability.stream})
    asset_classes = frozenset({AssetClass.crypto})
    connections: ClassVar[list[list[str]]] = []

    def _default_symbol(self, symbol, asset_class):
        return symbol

    async def get_quotes(self, items):
        return {s: _quote(s, D(100)) for s, _ in items}

    async def stream(self, items, **kw):
        type(self).connections.append([s for s, _ in items])
        index = {s: (RULE_SYMBOLS.index(s) if s in RULE_SYMBOLS else 0) for s, _ in items}
        k = 0
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            for canonical, _ in items:
                yield _quote(canonical, _price(index[canonical], k)
                             if canonical in RULE_SYMBOLS else D(100))
            k += 1
            await asyncio.sleep(max(0.0, start + k / RATE_HZ - loop.time()))


async def _campaign_delays(redis, seconds: float) -> tuple[list[float], Counter[str]]:
    """The delay of every campaign quote published on `ticks:` (receipt − quote time)."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(*(f"ticks:{s}" for s in CAMPAIGN_SYMBOLS))
    delays: list[float] = []
    seen: Counter[str] = Counter()
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    try:
        while loop.time() < end:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if not msg or msg["type"] != "message":
                continue
            q = loads_quote(msg["data"].decode())
            delays.append((datetime.now(UTC) - q.ts).total_seconds())
            seen[q.symbol] += 1
    finally:
        await pubsub.aclose()
    return delays, seen


async def test_stream_manager_unaffected(maker, redis, redis_url, fresh_db, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "load_stream", LoadStream)
    monkeypatch.setattr(LoadStream, "connections", [])
    async with maker() as s:
        s.add(Connector(name="load_stream", enabled=True))
        for capability in (Capability.quote, Capability.stream):
            await s.merge(ProviderPriority(asset_class=AssetClass.crypto,
                                           capability=capability, providers=["load_stream"]))
        await s.commit()
    for i, sym in enumerate(CAMPAIGN_SYMBOLS):
        await track(redis, [sym], source="orders" if i % 2 else "positions")
    async with httpx.AsyncClient() as http:
        mgr = StreamManager(MarketDataRouter(maker, redis, http), redis, maker,
                            reconcile_every_s=0.5)
        manager = asyncio.create_task(mgr.run())
        try:
            async def connected(n: int) -> None:
                deadline = time.monotonic() + 15
                while len((mgr.status()["streams"].get("load_stream:crypto") or {})
                          .get("symbols", [])) != n:
                    assert time.monotonic() < deadline, mgr.status()
                    await asyncio.sleep(0.05)
                await asyncio.sleep(1)  # first quotes out, polling over

            await connected(len(CAMPAIGN_SYMBOLS))
            idle, idle_seen = await _campaign_delays(redis, 6)

            await _rules(maker)
            await _warmup_bars(redis)
            async with scalper_process(fresh_db, redis, redis_url):  # tracks the rules
                await connected(len(CAMPAIGN_SYMBOLS) + N_RULES)
                loaded, loaded_seen = await _campaign_delays(redis, 6)
            assert not manager.done()
        finally:
            manager.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await manager

    expected = 6 * RATE_HZ
    for seen in (idle_seen, loaded_seen):  # every campaign quote published
        assert set(seen) == set(CAMPAIGN_SYMBOLS)
        assert min(seen.values()) >= expected * 0.75, seen
    idle.sort()
    loaded.sort()
    # the same delay: ±20%, with a 2 ms floor (a ~1 ms delay moves by more than 20% on
    # a busy machine without anything being slower)
    for name, a, b in (("median", statistics.median(idle), statistics.median(loaded)),
                       ("p95", _p(idle, 0.95), _p(loaded, 0.95))):
        assert abs(b - a) <= max(0.2 * a, 0.002), (
            f"campaign delay {name} {a * 1000:.2f} ms without rules, "
            f"{b * 1000:.2f} ms with them")
    print(f"\nstream: campaign delay median {statistics.median(idle) * 1000:.2f} / p95 "
          f"{_p(idle, 0.95) * 1000:.2f} ms without rules, median "
          f"{statistics.median(loaded) * 1000:.2f} / p95 {_p(loaded, 0.95) * 1000:.2f} ms "
          f"with {N_RULES}")
