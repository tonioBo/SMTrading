"""The `scalper` service (intraday mode B): prices only from the internal publication
(`quote:`/`ticks:` of `write_quote`, `bars1m:` of the stream manager), warm-up from the
minute history that never fires, idle with no active rule. Names are made up."""
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise
from pathlib import Path

import pytest
import redis.asyncio as aioredis
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.admin.system_health import OK, job_rows
from smtrading.autorules import main as scalper_main
from smtrading.autorules import rules
from smtrading.autorules import service as service_module
from smtrading.autorules.feed import PriceFeed, stream_state, warmup_start
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import ActiveRule
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.autorules.service import Scalper
from smtrading.autorules.tables import AutoTrade
from smtrading.common.db import make_engine
from smtrading.engine.models import Order
from smtrading.engine.quotesource import FakeMarketData
from smtrading.marketdata import provider as provider_module
from smtrading.marketdata import streaming
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.minute_bars import StreamBar, publish_minute_bar
from smtrading.marketdata.streaming import STATUS_KEY, TRACKED_KEY
from smtrading.marketdata.types import Bar, Quote
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.worker import jobstate

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)  # a Wednesday, 11:00 New York: NYSE open
BY = "admin@example.com"
COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _bar(ts: datetime, high: str = "100", close: str = "99.5") -> Bar:
    return Bar(ts=ts, open=D(99), high=D(high), low=D("98.5"), close=D(close),
               volume=D(1000))


def _quote(symbol: str, last: str, ts: datetime) -> Quote:
    p = D(last)
    return Quote(symbol=symbol, bid=p - D("0.01"), ask=p + D("0.01"), last=p, ts=ts,
                 provider="fake")


def _active(symbol: str = "AAPL", *, version: int = 1, params: dict | None = None,
            market: str = "NASDAQ") -> ActiveRule:
    from smtrading.autorules.models import validate_brakes, validate_params

    return ActiveRule(
        rule_id=uuid.uuid4(), version=version, portfolio_id=uuid.uuid4(), symbol=symbol,
        model="breakout", params=validate_params("breakout", params or {"lookback_min": 20}),
        qty=D(1), notional=None, brakes=validate_brakes({}, level="rule"),
        portfolio_brakes=validate_brakes({}, level="portfolio"), market=market,
        asset_class="us_equity", tick_size=D("0.01"), day_close_minutes=10,
        base_currency="USD")


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def sm(migrated_db):
    """Sessions on one connection inside a rolled-back transaction: the scalper's own
    short sessions see the test's rows (savepoints), nothing is ever committed."""
    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
        async with maker() as s:
            await run_seed(s)
            await s.commit()
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


async def _active_rule(sm, *, params: dict | None = None):
    async with sm() as s:
        p = await create_portfolio(s, name=f"S-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        p.auto_rules_enabled = True
        await s.flush()
        r = await rules.create_rule(s, portfolio_id=p.id, symbol="AAPL", model="breakout",
                                    params=params or {"lookback_min": 20}, qty=D(1), by=BY)
        await rules.activate(s, r.id, by=BY, now=NOW)
        await s.commit()
        return r.id


class Refusing:
    """Fails on every call (and records it), except the names in `allowed`."""

    def __init__(self, *allowed: str) -> None:
        self.allowed = set(allowed)
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            self.calls.append(name)
            if name in self.allowed:
                return 1
            raise AssertionError(f"unexpected call {name}")
        return call


class Intents:
    def __init__(self) -> None:
        self.entries: list[tuple] = []

    def __call__(self, sessionmaker, redis, qs, *, clock):
        return self

    async def enter(self, rule, runner, entry, quote):
        self.entries.append((rule, runner, entry, quote))


async def test_idle_without_active_rules(sm, monkeypatch):
    redis = Refusing("hset")  # JobRecorder's heartbeat
    router = Refusing()
    queries: list[int] = []

    async def counted(session):
        found = await rules.active_rules(session)
        queries.append(len(found))
        return found
    monkeypatch.setattr(service_module, "active_rules", counted)
    scalper = Scalper(sm, redis, router, reload_s=0.01)
    jobstate.bind(jobstate.JobRecorder(redis), "scalper")
    try:
        await scalper.reload()
        task = asyncio.create_task(scalper.run())
        await asyncio.sleep(0.05)  # several reload rounds
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        jobstate.bind(None, "scalper")
    assert len(queries) >= 4 and set(queries) == {0}  # reload + at least 3 loop rounds
    assert router.calls == []
    assert set(redis.calls) <= {"hset"} and redis.calls  # the heartbeat only


async def test_heartbeat_counts_active_rules(sm, redis):
    await _active_rule(sm)
    rec = jobstate.JobRecorder(redis)
    jobstate.bind(rec, "scalper")
    try:
        scalper = Scalper(sm, redis, FakeMarketData(), clock=lambda: NOW)
        await scalper.reload()
    finally:
        jobstate.bind(None, "scalper")
    _, jobs = await jobstate.read_jobs(redis)
    assert jobs["scalper"]["info"] == {"active_rules": 1}
    await scalper.close()


async def test_warmup_never_fires(sm, redis):
    rule_id = await _active_rule(sm)
    md = FakeMarketData()
    start = NOW - timedelta(minutes=60)
    bars = [_bar(start + timedelta(minutes=i)) for i in range(59)]
    bars.append(_bar(start + timedelta(minutes=59), high="101", close="100.9"))  # breakout
    md.set_bars("AAPL", bars)
    intents = Intents()
    scalper = Scalper(sm, redis, md, clock=lambda: NOW, executor_factory=intents)
    try:
        await scalper.reload()
        [(rule, runner)] = scalper.runners("AAPL")
        assert rule.rule_id == rule_id
        assert runner.series.count() == 25  # warm-up 20 + 5 kept
        assert runner.series.closed(1)[-1].ts == start + timedelta(minutes=59)
        assert intents.entries == []
        async with sm() as s:
            assert await s.scalar(select(func.count()).select_from(AutoTrade)) == 0
            assert await s.scalar(select(func.count()).select_from(Order)) == 0
        await scalper.on_event(_quote("AAPL", "100.5", NOW + timedelta(seconds=1)))
        assert intents.entries == []  # under the 20-minute high (101)
        await scalper.on_event(_quote("AAPL", "101.5", NOW + timedelta(seconds=2)))
        [(r, run, entry, q)] = intents.entries
        assert r.rule_id == rule_id and run is runner and isinstance(entry, Entry)
        assert entry.price == D("101.5") and entry.values["high"] == D(101)
        assert q.last == D("101.5")
        # the rule symbol is followed again (idempotent re-track on every refresh)
        assert await redis.sismember(f"{TRACKED_KEY}:auto_rules", "AAPL")
        assert await redis.sismember(TRACKED_KEY, "AAPL")
    finally:
        await scalper.close()


class RecordingRunner:
    def __init__(self, warmup: int) -> None:
        self.warmup = warmup
        self.bars: list[Bar] = []

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    def on_price(self, *a, **k):
        raise AssertionError("the warm-up never evaluates a price")


class BarsRouter:
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars
        self.asked: list[tuple] = []

    async def get_bars(self, symbol, timeframe, start, end):
        self.asked.append((symbol, str(timeframe), start, end))
        return [b for b in self.bars if start <= b.ts < end]


async def test_warmup_merges_history_and_stream(redis):
    t = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    rest = [_bar(t + timedelta(minutes=i)) for i in range(11)]  # 15:00 … 15:10
    router = BarsRouter(rest)
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(t + timedelta(minutes=9),
                                                           close="77")))
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(t + timedelta(minutes=11),
                                                           close="88")))
    now = t + timedelta(minutes=11, seconds=30)
    feed = PriceFeed(redis, router, clock=lambda: now)
    runner = RecordingRunner(warmup=20)
    await feed.warm_up(_active(), runner, now)
    [(sym, tf, start, end)] = router.asked
    assert (sym, tf, end) == ("AAPL", "1m", now)
    # session open 14:30Z, and at least warmup + 5 = 25 minutes before now
    assert start == datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
    got = [b.ts for b in runner.bars]
    assert got == sorted(set(got))  # no duplicates, oldest first
    assert got[-1] == t + timedelta(minutes=11)
    assert got[-2] == t + timedelta(minutes=10)
    assert [b.close for b in runner.bars if b.ts == t + timedelta(minutes=9)] == [D("99.5")]
    # a long warm-up starts from the previous session's open (session minutes only)
    router.asked.clear()
    await feed.warm_up(_active(), RecordingRunner(warmup=120), now)
    assert router.asked[0][2] == datetime(2026, 11, 24, 14, 30, tzinfo=UTC)
    # crypto: from midnight UTC
    router.asked.clear()
    await feed.warm_up(_active("BTC-USD", market="CRYPTO"), RecordingRunner(warmup=20), now)
    assert router.asked[0][2] == datetime(2026, 11, 25, tzinfo=UTC)
    await feed.close()


class FailingRouter:
    async def get_bars(self, *a, **k):
        raise RuntimeError("history unavailable")


async def test_warmup_reads_the_newest_stream_bars(redis):
    now = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)
    first = now - timedelta(minutes=1150)
    for i in range(1150):  # beyond BARS_MAXLEN: the stream is trimmed to ~1000-1100
        await publish_minute_bar(redis, StreamBar("BTC-USD", _bar(first + timedelta(minutes=i))))
    assert await redis.xlen("bars1m:BTC-USD") >= 1000
    feed = PriceFeed(redis, FailingRouter(), clock=lambda: now)
    runner = RecordingRunner(warmup=20)
    await feed.warm_up(_active("BTC-USD", market="CRYPTO"), runner, now)
    got = [b.ts for b in runner.bars]
    assert got[-1] == now - timedelta(minutes=1)  # the newest bar, not the 1000th oldest
    assert got == sorted(set(got)) and len(got) >= 1000
    assert all(b - a == timedelta(minutes=1) for a, b in pairwise(got))


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 25, 13, 0, tzinfo=UTC),   # before the open (08:00 New York)
    datetime(2026, 11, 25, 14, 35, tzinfo=UTC),  # 5 minutes after the open
])
async def test_warmup_from_the_previous_session(redis, now):
    prev_close = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
    assert warmup_start("NASDAQ", 20, now) == datetime(2026, 11, 24, 14, 30, tzinfo=UTC)
    rest = [_bar(prev_close - timedelta(minutes=30 - i)) for i in range(30)]
    feed = PriceFeed(redis, BarsRouter(rest), clock=lambda: now)
    runner = RuleRunner("breakout", {"lookback_min": 20}, market="NASDAQ",
                        tick_size=D("0.01"), session_exit_before=timedelta(minutes=12))
    await feed.warm_up(_active(), runner, now)
    assert runner.series.count() == 25 >= runner.warmup  # warmed: it can trade at once
    assert runner.series.closed(1)[-1].ts == prev_close - timedelta(minutes=1)


async def test_warmup_merges_previous_session_stream_bars(redis):
    now = datetime(2026, 11, 25, 14, 35, tzinfo=UTC)
    prev_close = datetime(2026, 11, 24, 21, 0, tzinfo=UTC)
    for i in range(20):  # yesterday's last 20 minutes, only in bars1m
        await publish_minute_bar(redis, StreamBar(
            "AAPL", _bar(prev_close - timedelta(minutes=20 - i))))
    today = datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
    rest = [_bar(today + timedelta(minutes=i)) for i in range(5)]  # REST: today only
    feed = PriceFeed(redis, BarsRouter(rest), clock=lambda: now)
    runner = RuleRunner("breakout", {"lookback_min": 20}, market="NASDAQ",
                        tick_size=D("0.01"), session_exit_before=timedelta(minutes=12))
    await feed.warm_up(_active(), runner, now)
    ts = [b.ts for b in runner.series.closed(25)]
    assert len(ts) == 25 and ts[0] == prev_close - timedelta(minutes=20)
    assert ts[-1] == today + timedelta(minutes=4)


def test_warmup_start_evening_and_crypto():
    evening = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)  # after the close: today is enough
    assert warmup_start("NASDAQ", 20, evening) == datetime(2026, 11, 25, 14, 30, tzinfo=UTC)
    assert warmup_start("CRYPTO", 20, datetime(2026, 11, 25, 0, 10, tzinfo=UTC)) \
        == datetime(2026, 11, 24, 23, 45, tzinfo=UTC)


async def test_shutdown_records_job_stopped(redis):
    rec = jobstate.JobRecorder(redis)
    stop = asyncio.Event()
    started = asyncio.Event()

    async def job():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(scalper_main.serve(stop, rec, job))
    await asyncio.wait_for(started.wait(), 2)
    stop.set()
    await asyncio.wait_for(task, 2)
    _, jobs = await jobstate.read_jobs(redis)
    assert jobs["scalper"]["state"] == "stopped"
    [row] = job_rows(jobs, None, time.time() + 3600)  # an hour later: still not late
    assert (row["status"], row["note"]) == (OK, "fermato")


async def test_reload_picks_new_version(sm, redis):
    rule_id = await _active_rule(sm, params={"lookback_min": 20})
    md = FakeMarketData()
    md.set_bars("AAPL", [_bar(NOW - timedelta(minutes=30 - i)) for i in range(30)])
    scalper = Scalper(sm, redis, md, clock=lambda: NOW)
    try:
        await scalper.reload()
        [(_, old)] = scalper.runners("AAPL")
        assert old.params["lookback_min"] == 20
        await scalper.reload()  # nothing changed: the same runner
        assert scalper.runners("AAPL")[0][1] is old
        async with sm() as s:
            await rules.pause(s, rule_id, by=BY)
            await rules.update_rule(s, rule_id, params={"lookback_min": 10}, by=BY)
            await rules.resume(s, rule_id, by=BY, now=NOW)
            await s.commit()
        await scalper.reload()
        [(rule, new)] = scalper.runners("AAPL")
        assert rule.version == 2 and new is not old
        assert new.params["lookback_min"] == 10
        assert new.series.count() == 15  # warmed up again (warm-up 10 + 5 kept)
        async with sm() as s:
            await rules.pause(s, rule_id, by=BY)
            await s.commit()
        await scalper.reload()
        assert scalper.runners("AAPL") == []
    finally:
        await scalper.close()


async def test_cache_quote_source_never_fetches(redis):
    fetcher = Refusing()
    fx = FakeMarketData()
    fx.set_fx("EUR", "USD", D("1.25"))

    class Router:
        async def get_quotes(self, *a, **k):
            fetcher.calls.append("get_quotes")
            raise AssertionError("never asks the providers")

        get_fx_rate = fx.get_fx_rate
        get_calendar = fx.get_calendar

    await write_quote(redis, _quote("AAPL", "190", datetime.now(UTC)))
    qs = CacheQuoteSource(redis, Router())
    got = await qs.get_quotes(["AAPL", "MSFT"])
    assert list(got) == ["AAPL"] and got["AAPL"].last == D(190)
    assert await qs.get_quotes(["MSFT"]) == {}
    assert fetcher.calls == []
    assert await qs.get_fx_rate("EUR", "USD") == D("1.25")


def test_stream_state():
    status = {"streams": {"alpaca:us_equity": {"symbols": ["AAPL", "MSFT"],
                                               "state": "connected", "note": None,
                                               "bars": ["AAPL"]},
                          "binance:crypto": {"symbols": ["BTC-USD"], "state": "backoff",
                                             "note": None}},
              "polling": ["BTC-USD", "SPY"], "owner": "x"}
    assert stream_state(status, "AAPL") == "stream"
    assert stream_state(status, "MSFT") == "stream"
    assert stream_state(status, "SPY") == "polling"
    assert stream_state(status, "BTC-USD") == "polling"  # its stream is not connected
    assert stream_state(status, "NVDA") == "none"
    assert stream_state(None, "AAPL") == "none"
    assert stream_state({}, "AAPL") == "none"


async def test_scalper_never_opens_streams(sm, redis, monkeypatch):
    await _active_rule(sm)
    opened: list[str] = []

    def classes(base):
        for c in base.__subclasses__():
            yield c
            yield from classes(c)

    for cls in {provider_module.DataProvider, *classes(provider_module.DataProvider)}:
        if "stream" in vars(cls):
            async def stream(self, *a, _n=cls.__name__, **k):
                opened.append(_n)
                raise AssertionError("no stream from the scalper")
            monkeypatch.setattr(cls, "stream", stream)

    def no_manager(self, *a, **k):
        opened.append("StreamManager")
        raise AssertionError("no stream manager in the scalper")
    monkeypatch.setattr(streaming.StreamManager, "__init__", no_manager)

    md = FakeMarketData()
    md.set_bars("AAPL", [_bar(NOW - timedelta(minutes=30 - i)) for i in range(30)])
    await redis.set(STATUS_KEY, json.dumps({"streams": {}, "polling": ["AAPL"]}))
    intents = Intents()
    scalper = Scalper(sm, redis, md, clock=lambda: NOW, reload_s=0.2,
                      executor_factory=intents)
    task = asyncio.create_task(scalper.run())
    await asyncio.sleep(0.3)
    # a tick through the existing publication reaches the rule
    await write_quote(redis, _quote("AAPL", "150", NOW + timedelta(seconds=1)))
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(NOW, high="100.2")))
    await asyncio.sleep(0.7)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened == []
    assert len(intents.entries) == 1  # the published tick, above the high
    [(_, runner)] = scalper.runners("AAPL")
    assert runner.series.closed(1)[-1].ts == NOW  # the published minute bar
    for name in ("StreamManager", "OrderProcessor", "BrokerSync"):
        assert not hasattr(scalper_main, name)


def test_compose_has_scalper_service():
    compose = yaml.safe_load(COMPOSE.read_text())
    svc = compose["services"]["scalper"]
    assert svc["command"] == ["python", "-m", "smtrading.autorules.main"]
    assert "ports" not in svc
    assert svc["image"] == compose["services"]["worker"]["image"]  # the same image (<<: *app)
    assert "smtrading.autorules.main" in COMPOSE.read_text()

