"""The minute bars of a rule symbol whose stream carries none (crypto on Kraken, a bars
stream refused, a silent bars stream): fetched by the scalper through the router's REST
shortly after each minute close, so a runner never trades on bars frozen at its warm-up.
Names are made up."""
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
import redis.asyncio as aioredis

from smtrading.autorules import service as service_module
from smtrading.autorules.models import validate_brakes, validate_params
from smtrading.autorules.rules import ActiveRule
from smtrading.autorules.runner import RuleRunner
from smtrading.autorules.service import Scalper
from smtrading.marketdata.minute_bars import StreamBar
from smtrading.marketdata.streaming import CONNECTED
from smtrading.marketdata.types import Bar

T0 = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)  # a Wednesday, 11:00 New York: NYSE open


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _bar(ts: datetime, close: str = "100") -> Bar:
    c = D(close)
    return Bar(ts=ts, open=c, high=c + 1, low=c - 1, close=c, volume=D(10))


def _rule(symbol: str, market: str, asset_class: str) -> ActiveRule:
    return ActiveRule(
        rule_id=uuid.uuid4(), version=1, portfolio_id=uuid.uuid4(), symbol=symbol,
        model="mean_reversion", params=validate_params("mean_reversion", {}), qty=D(1),
        notional=None, brakes=validate_brakes({}, level="rule"),
        portfolio_brakes=validate_brakes({}, level="portfolio"), market=market,
        asset_class=asset_class, tick_size=D("0.01"), day_close_minutes=10,
        base_currency="USD")


def _runner(rule: ActiveRule, until: datetime) -> RuleRunner:
    """A runner warmed up with bars up to the minute before `until`."""
    r = RuleRunner(rule.model, rule.params, market=rule.market, tick_size=rule.tick_size,
                   session_exit_before=timedelta(minutes=12))
    for i in range(r.warmup + 5, 0, -1):
        r.on_bar(_bar(until - timedelta(minutes=i)))
    return r


class Router:
    """The router's `get_bars`: every bar closed by the clock's time, or an error."""

    def __init__(self, clock: Clock, *, fail: bool = False) -> None:
        self.clock = clock
        self.fail = fail
        self.asked: list[tuple] = []

    async def get_bars(self, symbol, timeframe, start, end):
        self.asked.append((symbol, str(timeframe), start, end))
        if self.fail:
            raise RuntimeError("provider down")
        out, ts = [], start.replace(second=0, microsecond=0)
        while ts + timedelta(minutes=1) <= min(end, self.clock.now):
            out.append(_bar(ts, close="200"))
            ts += timedelta(minutes=1)
        return out


def _status(stream_symbols: list[str], bars: list[str] | None) -> dict:
    s = {"state": CONNECTED, "symbols": stream_symbols}
    if bars is not None:
        s["bars"] = bars
    return {"streams": {"k": s}, "polling": []}


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def _scalper(redis, router, clock, *pairs, status) -> Scalper:
    sc = Scalper(None, redis, router, clock=clock, executor_factory=None)
    for rule, runner in pairs:
        sc._rules[rule.rule_id] = (rule, runner)
    sc._index()
    sc.stream_status = status
    return sc


def _last(runner: RuleRunner) -> datetime:
    return runner.series.closed(1)[-1].ts


async def test_crypto_without_stream_bars_gets_rest_bars_each_minute(redis):
    clock = Clock(T0 + timedelta(seconds=30))
    rule, other = _rule("BTC-USD", "CRYPTO", "crypto"), _rule("BTC-USD", "CRYPTO", "crypto")
    r1, r2 = _runner(rule, T0), _runner(other, T0)
    router = Router(clock)
    sc = _scalper(redis, router, clock, (rule, r1), (other, r2),
                  status=_status(["BTC-USD"], None))
    clock.now = T0 + timedelta(minutes=1, seconds=2)  # before close + 5 s: not yet
    await sc.poll_bars()
    assert router.asked == []
    clock.now = T0 + timedelta(minutes=1, seconds=6)
    await sc.poll_bars()
    [(sym, tf, _, end)] = router.asked  # one call for the two rules of the symbol
    assert (sym, tf, end) == ("BTC-USD", "1m", clock.now)
    assert _last(r1) == _last(r2) == T0  # the just-closed minute
    assert r1.series.closed(1)[-1].close == D(200)
    await sc.poll_bars()  # the same minute: no second call
    assert len(router.asked) == 1
    clock.now = T0 + timedelta(minutes=2, seconds=7)
    await sc.poll_bars()
    assert len(router.asked) == 2 and _last(r1) == T0 + timedelta(minutes=1)


async def test_refused_bars_stream_polls_too(redis):
    """Equity on a connected stream but without bars (the bars stream refused)."""
    clock = Clock(T0 + timedelta(minutes=1, seconds=6))
    rule = _rule("AAPL", "NASDAQ", "us_equity")
    runner = _runner(rule, T0)
    router = Router(clock)
    sc = _scalper(redis, router, clock, (rule, runner), status=_status(["AAPL"], None))
    await sc.poll_bars()
    assert [a[0] for a in router.asked] == ["AAPL"] and _last(runner) == T0


async def test_alpaca_stream_bars_do_not_poll(redis):
    clock = Clock(T0 + timedelta(seconds=10))
    rule = _rule("AAPL", "NASDAQ", "us_equity")
    runner = _runner(rule, T0)
    router = Router(clock)
    sc = _scalper(redis, router, clock, (rule, runner),
                  status=_status(["AAPL"], ["AAPL"]))
    for i in range(1, 4):  # a stream bar every minute: never a REST call
        clock.now = T0 + timedelta(minutes=i, seconds=1)
        await sc.on_event(StreamBar("AAPL", _bar(T0 + timedelta(minutes=i - 1))))
        clock.now = T0 + timedelta(minutes=i, seconds=6)
        await sc.poll_bars()
    assert router.asked == []
    # the bars stream goes silent for more than 2 minutes in the session: REST
    clock.now = T0 + timedelta(minutes=4, seconds=6)
    await sc.poll_bars()
    assert router.asked == []  # last stream bar at 16:03:01: 1 min 5 s ago
    clock.now = T0 + timedelta(minutes=5, seconds=6)
    await sc.poll_bars()  # 2 min 5 s without a stream bar
    assert [a[0] for a in router.asked] == ["AAPL"]
    assert _last(runner) == T0 + timedelta(minutes=4)


async def test_closed_market_no_calls(redis):
    """An equity rule without stream bars, Thanksgiving: nothing to ask."""
    day = datetime(2026, 11, 26, 16, 1, 6, tzinfo=UTC)
    clock = Clock(day)
    rule = _rule("AAPL", "NASDAQ", "us_equity")
    router = Router(clock)
    sc = _scalper(redis, router, clock, (rule, _runner(rule, T0)),
                  status=_status(["AAPL"], None))
    await sc.poll_bars()
    assert router.asked == []


async def test_no_rules_no_calls(redis):
    clock = Clock(T0 + timedelta(minutes=1, seconds=6))
    router = Router(clock)
    sc = _scalper(redis, router, clock, status=_status([], None))
    await sc.poll_bars()
    assert router.asked == []


async def test_rest_failure_logged_once_never_raises(redis, caplog):
    clock = Clock(T0 + timedelta(minutes=1, seconds=6))
    rule = _rule("BTC-USD", "CRYPTO", "crypto")
    runner = _runner(rule, T0)
    router = Router(clock, fail=True)
    sc = _scalper(redis, router, clock, (rule, runner), status=_status(["BTC-USD"], None))
    caplog.set_level(logging.WARNING, logger=service_module.__name__)

    def logged() -> int:
        return sum("BTC-USD" in r.getMessage() and "REST" in r.getMessage()
                   for r in caplog.records)

    for i in range(1, 5):
        clock.now = T0 + timedelta(minutes=i, seconds=6)
        await sc.poll_bars()
    assert len(router.asked) == 4 and logged() == 1
    assert _last(runner) == T0 - timedelta(minutes=1)
    clock.now = T0 + timedelta(minutes=1 + service_module.REST_BARS_LOG_EVERY_S // 60,
                               seconds=7)
    await sc.poll_bars()
    assert logged() == 2


async def test_rest_bars_start_line_once_then_quarter_hour_summary(redis, caplog):
    clock = Clock(T0 + timedelta(minutes=1, seconds=6))
    rule = _rule("BTC-USD", "CRYPTO", "crypto")
    router = Router(clock)
    sc = _scalper(redis, router, clock, (rule, _runner(rule, T0)),
                  status=_status(["BTC-USD"], None))
    caplog.set_level(logging.INFO, logger=service_module.__name__)

    def lines() -> list[str]:
        return [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("BTC-USD: barre al minuto")]

    for i in range(1, 11):  # ten minutes: the start line only
        clock.now = T0 + timedelta(minutes=i, seconds=6)
        await sc.poll_bars()
    assert lines() == ["BTC-USD: barre al minuto dal fornitore (il flusso non le dà)"]
    clock.now = T0 + timedelta(minutes=1 + service_module.REST_BARS_SUMMARY_S // 60,
                               seconds=6)
    await sc.poll_bars()
    assert len(lines()) == 2 and "ultima barra" in lines()[1]
    router.fail = True  # a failure, then recovery: the start line again
    clock.now += timedelta(minutes=1)
    await sc.poll_bars()
    router.fail = False
    clock.now += timedelta(minutes=1)
    await sc.poll_bars()
    assert lines()[-1].endswith("(il flusso non le dà)") and len(lines()) == 3
