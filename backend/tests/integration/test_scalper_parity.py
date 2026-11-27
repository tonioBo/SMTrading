"""Parity of the live engine and the one-minute replay on the same series.

The same 120 one-minute bars of AAPL go (a) through `replay` with no spread, no delay and
no fees and (b) through the real Scalper (its Executor and Tracker), the real BrokerSync
and the real OrderProcessor (stop and target) on an alpaca_paper link whose HTTP goes to
the FakeLiveBroker: every price of `price_path` becomes a quote (bid = ask = last = p),
the broker fills a market order at that price (stamped with its time), then the bar goes
to the runner. Two triggers: one exits at the target, one at the time limit. Names are
made up."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules import rules
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.replay import price_path, replay
from smtrading.autorules.rules import ActiveRule, active_rules
from smtrading.autorules.runner import SESSION_EXIT_MARGIN_MIN, RuleRunner
from smtrading.autorules.service import Scalper
from smtrading.autorules.tables import CLOSED, AutoTrade
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import HEARTBEAT_KEY, BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.minute_bars import StreamBar
from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY
from smtrading.marketdata.types import Bar, Quote
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

START = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # Wednesday, 10:00 New York
BY = "admin@example.com"
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}
SYMBOL = "AAPL"
PARAMS = {"lookback_min": 20, "max_hold_s": 120}
TICK = D("0.0001")  # AAPL's tick in the seed
FROZEN = {"now": START}


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _frozen_risk_clock(monkeypatch):
    """The risk check reads the wall clock for a quote's age: it sees FROZEN["now"]."""
    import smtrading.risk.checks as risk_checks

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            now = FROZEN["now"]
            return now if tz is None else now.astimezone(tz)

    FROZEN["now"] = START
    monkeypatch.setattr(risk_checks, "datetime", _Frozen)


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    m = async_sessionmaker(engine, expire_on_commit=False)
    async with m() as s:
        await run_seed(s)
        await s.commit()
    yield m
    await engine.dispose()


@pytest.fixture
def router():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_fx("USD", "EUR", D("0.9"))
    return q


class Broker:
    """The FakeLiveBroker filling market orders at `price`, each fill stamped with the
    time of the price that filled it (the broker's `filled_at`)."""

    def __init__(self) -> None:
        self.fake = FakeLiveBroker(price=D(200))
        self.fake.alpaca.cash = D(100000)
        self.fill_time: datetime = START
        self._stamps: dict[str, str] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        resp = self.fake._handle(request)
        for o in self.fake.alpaca.orders.values():
            if o["status"] == "filled" and o["id"] not in self._stamps:
                self._stamps[o["id"]] = self.fill_time.isoformat().replace("+00:00", "Z")
            if o["id"] in self._stamps:
                o["filled_at"] = self._stamps[o["id"]]
        if not resp.content or not resp.headers.get("content-type", "").startswith(
                "application/json"):
            return resp
        data = json.loads(resp.content)
        for o in data if isinstance(data, list) else [data]:
            if isinstance(o, dict) and o.get("id") in self._stamps:
                o["filled_at"] = self._stamps[o["id"]]
        return httpx.Response(resp.status_code, json=data)


@pytest.fixture
async def broker():
    b = Broker()
    async with httpx.AsyncClient(transport=httpx.MockTransport(b.handle)) as http:
        b.http = http
        yield b


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _bar(minute: int, o, h, low, c) -> Bar:
    return Bar(ts=START + timedelta(minutes=minute), open=D(o), high=D(h), low=D(low),
               close=D(c), volume=D(100))


def series() -> list[Bar]:
    """120 bars: flat at 200 (warm-up), a breakout at 15:20:30 (ask 200.50: stop 199.8985,
    target 201.5025) whose next bar reaches the target exactly; flat at 201.40, a second
    breakout at 16:00:30 (201.80: stop 201.1946, target 202.809), then flat at 201.70 until
    the time limit (120 s) and the end."""
    bars = [_bar(i, 200, 200, 200, 200) for i in range(20)]
    bars.append(_bar(20, "200", "200.50", "200", "200.40"))
    bars.append(_bar(21, "200.40", "201.5025", "200.40", "201.40"))
    bars += [_bar(i, "201.40", "201.40", "201.40", "201.40") for i in range(22, 60)]
    bars.append(_bar(60, "201.40", "201.80", "201.40", "201.70"))
    bars += [_bar(i, "201.70", "201.70", "201.70", "201.70") for i in range(61, 120)]
    assert len(bars) == 120
    return bars


def _zero_fees() -> FeeRuleData:
    return FeeRuleData(
        id=uuid4(), scope=FeeScope.global_, scope_ref=None, side=FeeSide.both,
        currency="USD", fixed=D(0), pct=D(0), min_fee=None, max_fee=None, tiers=None,
        fx_pct=D(0), priority=0, active=True, created_at=START)


def _tick(v: D) -> D:
    return v.quantize(TICK, rounding=ROUND_HALF_UP)


def _account(at: datetime) -> dict:
    return {"buying_power": "100000", "non_marginable_buying_power": "100000",
            "multiplier": "2", "equity": "100000", "trading_blocked": False,
            "account_blocked": False, "read_at": at.isoformat()}


async def _rule(maker) -> ActiveRule:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        p.auto_rules_enabled = True
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper", dict(CFG))
        await link_portfolio(s, p.id, link.id)
        link.pdt_state, link.pdt_state_at = _account(START), START
        await s.flush()
        r = await rules.create_rule(s, portfolio_id=p.id, symbol=SYMBOL, model="breakout",
                                    params=PARAMS, qty=D(1), notional=None, by=BY)
        await rules.activate(s, r.id, by=BY, now=START)
        await s.commit()
        [rule] = [a for a in await active_rules(s) if a.rule_id == r.id]
        return rule


async def _fresh_account(maker, rule: ActiveRule, at: datetime) -> None:
    """The account state read just now (its age is checked against the clock)."""
    async with maker() as s:
        link_id = (await s.get(Portfolio, rule.portfolio_id)).broker_link_id
        await s.execute(update(BrokerLink).where(BrokerLink.id == link_id).values(
            pdt_state=_account(at), pdt_state_at=at))
        await s.commit()


async def _live(maker, redis, router, broker, rule: ActiveRule, bars: list[Bar]):
    qs = CacheQuoteSource(redis, router)
    clock = Clock(START)
    sc = Scalper(maker, redis, router, clock=clock)
    runner = RuleRunner(rule.model, rule.params, market=rule.market, tick_size=rule.tick_size,
                        session_exit_before=timedelta(
                            minutes=rule.day_close_minutes + SESSION_EXIT_MARGIN_MIN))
    sc._rules[rule.rule_id] = (rule, runner)
    sc._index()
    status = json.dumps({"streams": {"alpaca": {"state": CONNECTED, "symbols": [SYMBOL],
                                                "bars": [SYMBOL]}}, "polling": []})
    await redis.set(STATUS_KEY, status)
    for bar in bars:
        await _fresh_account(maker, rule, bar.ts)
        for ts, p in price_path(bar):
            clock.now = FROZEN["now"] = broker.fill_time = ts
            broker.fake.price = p
            await redis.set(HEARTBEAT_KEY, (ts - timedelta(seconds=1)).isoformat())
            q = Quote(symbol=SYMBOL, bid=p, ask=p, last=p, ts=ts, provider="fake")
            await write_quote(redis, q, fetched_at=ts)
            await OrderProcessor(maker, redis, qs, clock=Clock(ts)).on_tick(SYMBOL, q)
            await sc.on_event(q)
            for _ in range(2):  # an entry or exit sent and filled, then a time exit
                for _ in range(2):
                    await BrokerSync(maker, redis, qs, broker.http, clock=Clock(ts),
                                     interval_s=30).tick()
                await sc.track()
        await sc.on_event(StreamBar(SYMBOL, bar))
    async with maker() as s:
        return list(await s.scalars(select(AutoTrade).where(
            AutoTrade.rule_id == rule.rule_id).order_by(AutoTrade.id)))


async def test_engine_matches_replay(maker, redis, router, broker):
    bars = series()
    rule = await _rule(maker)
    assert rule.market in ("NASDAQ", "NYSE") and rule.tick_size == TICK
    replayed = replay(bars, model=rule.model, params=PARAMS, market=rule.market,
                      tick_size=rule.tick_size, qty=D(1), notional=None,
                      fee_rule=_zero_fees(), spread_pct=D(0), delay_s=0,
                      day_close_minutes=rule.day_close_minutes)
    expected = [(t.entry_ts, _tick(t.entry_price), t.exit_reason, _tick(t.exit_price))
                for t in replayed.trades]
    assert expected == [
        (START + timedelta(minutes=20, seconds=30), D("200.5000"), "target", D("201.5025")),
        (START + timedelta(minutes=60, seconds=30), D("201.80"), "time", D("201.70")),
    ]
    live = await _live(maker, redis, router, broker, rule, bars)
    assert all(t.status == CLOSED for t in live), [(t.status, t.error) for t in live]
    got = [(t.filled_at.replace(microsecond=0), _tick(t.fill_price), t.exit_reason,
            _tick(t.exit_price)) for t in live]
    assert got == expected
    assert len(live) == len(replayed.trades)
    # the exits' times agree too (only milliseconds of execution may differ)
    assert [t.exit_filled_at.replace(microsecond=0) for t in live] == [
        t.exit_ts for t in replayed.trades]
