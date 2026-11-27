"""Mode B after the entry: the Tracker (fills and their measures, the exits and their
reason, fees and the net result in the portfolio's currency), the time and end-of-session
exits sent by the scalper (one per trade, never refused by the day-trade checks), the
stale limit entry canceled, the rebuild after a restart and the evening summary.

The real Executor, Tracker, BrokerSync and OrderProcessor on an alpaca_paper link whose
HTTP goes to the FakeLiveBroker (a wrapper stamps the broker's `filled_at` on the fills,
the time the test chooses). Quotes from the Redis cache, exchange rates from a
FakeQuoteSource. Names are made up."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules import rules
from smtrading.autorules.executor import Executor
from smtrading.autorules.guards import count_blocked
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import ActiveRule, active_rules
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.autorules.service import Scalper
from smtrading.autorules.summary import send_due_summaries
from smtrading.autorules.tables import CANCELED, CLOSED, OPEN, AutoTrade
from smtrading.autorules.tracker import Tracker
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import HEARTBEAT_KEY, BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import BrokerState, Fill, Order, OrderStatus, OrderType
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import close_position
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import Instrument
from smtrading.intraday.day_close import DayCloser
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)  # Wednesday, 11:00 New York: NYSE open
BY = "admin@example.com"
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}
FROZEN = {"now": NOW}


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

    FROZEN["now"] = NOW
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


@pytest.fixture
def qs(redis, router):
    return CacheQuoteSource(redis, router)


class Broker:
    """The FakeLiveBroker, its fills stamped with `fill_time` (the broker's `filled_at`)
    when they happen."""

    def __init__(self, price: str) -> None:
        self.fake = FakeLiveBroker(price=D(price))
        self.fake.alpaca.cash = D(100000)
        self.fill_time: datetime | None = None
        self.partial_sell: D | None = None  # a sell fills only this much, the rest canceled
        self._stamps: dict[str, str] = {}

    def _partial(self, order_id: str) -> None:
        """The sell just filled in full by the fake: only `partial_sell` of it."""
        o = self.fake.alpaca.orders[order_id]
        rest = D(o["qty"]) - self.partial_sell
        self.fake.alpaca.positions[o["symbol"]] = (
            self.fake.alpaca.positions.get(o["symbol"], D(0)) + rest)
        o.update(filled_qty=str(self.partial_sell), status="canceled")

    def _stamp(self) -> None:
        for o in self.fake.alpaca.orders.values():
            if o["status"] == "filled" and o["id"] not in self._stamps:
                ts = self.fill_time.isoformat().replace("+00:00", "Z") if self.fill_time \
                    else None
                self._stamps[o["id"]] = ts
            if o["id"] in self._stamps:
                o["filled_at"] = self._stamps[o["id"]]

    def handle(self, request: httpx.Request) -> httpx.Response:
        resp = self.fake._handle(request)
        if (self.partial_sell is not None and request.method == "POST"
                and request.url.path == "/v2/orders" and resp.status_code == 200):
            placed = json.loads(resp.content)
            if placed["side"] == "sell" and placed["status"] == "filled":
                self._partial(placed["id"])
                resp = httpx.Response(200, json=self.fake.alpaca.orders[placed["id"]])
        self._stamp()
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
    b = Broker("200.00")
    async with httpx.AsyncClient(transport=httpx.MockTransport(b.handle)) as http:
        b.http = http
        yield b


def account(bp="100000", at=NOW - timedelta(minutes=1)):
    return {"buying_power": bp, "non_marginable_buying_power": bp, "multiplier": "2",
            "equity": "100000", "trading_blocked": False, "account_blocked": False,
            "read_at": at.isoformat()}, at


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _rule(maker, symbol="AAPL", *, qty=D(1), params=None, **pf) -> ActiveRule:
    async with maker() as s:
        p = await create_portfolio(s, name=f"S-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        p.auto_rules_enabled = True
        for k, v in pf.items():
            setattr(p, k, v)
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper", dict(CFG))
        await link_portfolio(s, p.id, link.id)
        link.pdt_state, link.pdt_state_at = account()
        await s.flush()
        r = await rules.create_rule(s, portfolio_id=p.id, symbol=symbol, model="breakout",
                                    params={"lookback_min": 20, **(params or {})},
                                    qty=qty, notional=None, by=BY)
        await rules.activate(s, r.id, by=BY, now=NOW)
        await s.commit()
        [rule] = [a for a in await active_rules(s) if a.rule_id == r.id]
        return rule


def _runner(rule: ActiveRule) -> RuleRunner:
    return RuleRunner(rule.model, rule.params, market=rule.market, tick_size=rule.tick_size,
                      session_exit_before=timedelta(minutes=rule.day_close_minutes + 2))


async def _ready(redis, now, symbol="AAPL") -> None:
    await redis.set(STATUS_KEY, json.dumps({
        "streams": {"alpaca": {"state": CONNECTED, "symbols": [symbol], "bars": [symbol]}},
        "polling": []}))
    await redis.set(HEARTBEAT_KEY, (now - timedelta(seconds=1)).isoformat())


async def _quote(redis, ts, bid="199.90", ask="200.00", symbol="AAPL") -> Quote:
    q = Quote(symbol=symbol, bid=D(bid), ask=D(ask), last=D(ask), ts=ts, provider="fake")
    await write_quote(redis, q, fetched_at=ts)
    return q


def _sync(maker, redis, qs, broker, now) -> BrokerSync:
    return BrokerSync(maker, redis, qs, broker.http, clock=Clock(now), interval_s=30)


async def _send(maker, redis, qs, broker, rule, runner, *, now=NOW, sent_at=None,
                bid="199.90", ask="200.00") -> AutoTrade:
    """An entry triggered at `now`, written at `sent_at` (default `now`)."""
    await _ready(redis, sent_at or now)
    q = await _quote(redis, now, bid, ask)
    trade = await Executor(maker, redis, qs, clock=Clock(sent_at or now)).enter(
        rule, runner, Entry(q.ts, q.ask, {}), q)
    assert trade is not None and trade.entry_order_id is not None, trade
    return trade


async def _open(maker, redis, qs, broker, rule, *, now=NOW, fill_at=None):
    """An entry sent at `now` and filled by the broker at `fill_at` (default now + 1 s),
    then seen by a Tracker: (trade, runner)."""
    fill_at = fill_at or now + timedelta(seconds=1)
    runner = _runner(rule)
    trade = await _send(maker, redis, qs, broker, rule, runner, now=now)
    broker.fill_time = fill_at
    await _sync(maker, redis, qs, broker, now).tick()
    await Tracker(maker, redis, qs, clock=Clock(fill_at)).step({rule.rule_id: runner})
    row = await _trade(maker, trade.id)
    assert row.status == OPEN and row.filled_at == fill_at
    assert runner.state == "long"
    return row, runner


async def _trade(maker, trade_id) -> AutoTrade:
    async with maker() as s:
        return await s.get(AutoTrade, trade_id)


async def _orders(maker, rule) -> list[Order]:
    async with maker() as s:
        return list(await s.scalars(
            select(Order).where(Order.portfolio_id == rule.portfolio_id)
            .order_by(Order.created_at, Order.role)))


async def _autox(maker, rule) -> list[Order]:
    return [o for o in await _orders(maker, rule)
            if (o.client_order_id or "").startswith("smt-autox-")]


def _scalper(maker, redis, router, clock, rule, runner) -> Scalper:
    """A Scalper with the rule's runner already in place (no warm-up)."""
    sc = Scalper(maker, redis, router, clock=clock)
    sc._rules[rule.rule_id] = (rule, runner)
    sc._index()
    return sc


async def _fees(maker, *order_ids) -> tuple[D, list[str]]:
    """The sum of the fills' fees and their currencies."""
    async with maker() as s:
        total = await s.scalar(select(func.coalesce(func.sum(Fill.fee_amount), 0))
                               .where(Fill.order_id.in_(order_ids)))
        ccys = await s.scalars(select(Fill.fee_currency).distinct()
                               .where(Fill.order_id.in_(order_ids)))
        return total, sorted(ccys)


async def _settle(maker, redis, qs, broker, now, n=2) -> None:
    for _ in range(n):
        await _sync(maker, redis, qs, broker, now).tick()


# --- the entry fill -----------------------------------------------------------------------

async def test_fill_measured(maker, redis, qs, broker):
    broker.fake.price = D("100.02")
    rule = await _rule(maker)
    runner = _runner(rule)
    t0 = NOW
    trade = await _send(maker, redis, qs, broker, rule, runner, now=t0,
                        sent_at=t0 + timedelta(milliseconds=100), bid="99.99", ask="100.00")
    broker.fill_time = t0 + timedelta(milliseconds=600)
    await _sync(maker, redis, qs, broker, t0 + timedelta(milliseconds=250)).tick()
    await Tracker(maker, redis, qs, clock=Clock(t0 + timedelta(seconds=1))).step(
        {rule.rule_id: runner})
    row = await _trade(maker, trade.id)
    assert row.status == OPEN
    assert row.ms_trigger_to_send == 100
    assert row.broker_sent_at == t0 + timedelta(milliseconds=250)
    assert row.ms_send_to_broker == 150
    assert row.ms_broker_to_fill == 350
    assert row.fill_price == D("100.02") and row.qty == D(1)
    assert row.entry_slippage_bps == D(2)
    assert row.fill_ts_source == "broker"
    assert row.filled_at == t0 + timedelta(milliseconds=600)
    assert runner.state == "long" and runner.filled_at == row.filled_at
    assert runner.fill_price == D("100.02")


async def test_target_exit_closes_trade(maker, redis, qs, broker):
    rule = await _rule(maker)
    assert rule.base_currency == "EUR"
    row, runner = await _open(maker, redis, qs, broker, rule)
    tp = next(o for o in await _orders(maker, rule) if o.role == "take_profit")
    later = NOW + timedelta(seconds=20)
    FROZEN["now"] = later
    broker.fake.price = tp.limit_price
    broker.fill_time = later + timedelta(milliseconds=300)
    q = await _quote(redis, later, bid=str(tp.limit_price), ask=str(tp.limit_price + D("0.1")))
    await OrderProcessor(maker, redis, qs, clock=Clock(later)).on_tick("AAPL", q)
    await _settle(maker, redis, qs, broker, later)
    await Tracker(maker, redis, qs, clock=Clock(later + timedelta(seconds=1))).step(
        {rule.rule_id: runner})
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "target"
    assert closed.exit_order_id == tp.id
    assert closed.exit_expected == tp.limit_price and closed.exit_price == tp.limit_price
    assert closed.exit_filled_at == later + timedelta(milliseconds=300)
    fees, [fee_ccy] = await _fees(maker, row.entry_order_id, tp.id)
    assert fees > 0 and closed.fees == fees and closed.fees_currency == fee_ccy
    gross = (tp.limit_price - row.fill_price) * row.qty  # in USD
    fee_rate = {"USD": D("0.9"), "EUR": D(1)}[fee_ccy]
    assert closed.net_currency == "EUR"
    assert closed.net_result == gross * D("0.9") - fees * fee_rate
    assert closed.exit_slippage_bps == D(0)
    assert runner.state == "flat" and runner.last_exit == closed.exit_filled_at


# --- the exits the scalper sends ----------------------------------------------------------

async def test_time_exit_sent_by_scalper(maker, redis, qs, broker, router):
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    clock = Clock(row.filled_at + timedelta(seconds=59))
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    assert await _autox(maker, rule) == []
    clock.now = row.filled_at + timedelta(seconds=60)
    await _quote(redis, clock.now, bid="200.50", ask="200.60")
    await sc.track()
    [exit_order] = await _autox(maker, rule)
    assert exit_order.client_order_id == f"smt-autox-{row.id}"
    assert exit_order.type == OrderType.market and exit_order.reduce_only
    assert exit_order.side.value == "sell" and exit_order.qty == row.qty
    assert exit_order.status != OrderStatus.rejected
    children = [o for o in await _orders(maker, rule)
                if o.role in ("take_profit", "stop_loss")]
    assert children and all(o.status == OrderStatus.canceled for o in children)
    sent = await _trade(maker, row.id)
    assert sent.exit_reason == "time" and sent.exit_order_id == exit_order.id
    assert sent.exit_trigger_at == clock.now and sent.exit_expected == D("200.50")
    # the runner repeats the Exit until the fill: never a second order
    for _ in range(3):
        await sc.track()
        await sc.on_event(await _quote(redis, clock.now, bid="200.40", ask="200.50"))
    assert len(await _autox(maker, rule)) == 1
    broker.fake.price = D("200.40")
    broker.fill_time = clock.now + timedelta(milliseconds=400)
    await _settle(maker, redis, qs, broker, clock.now)
    clock.now += timedelta(seconds=1)
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "time"
    assert closed.exit_price == D("200.40")
    assert closed.exit_filled_at == broker.fill_time
    assert runner.state == "flat"


async def test_refused_exit_replaced(maker, redis, qs, broker, router):
    """A time exit the broker refuses leaves the position open: the next Exit sends a
    new order (`smt-autox-{trade}-1`), never two at once."""
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    clock = Clock(row.filled_at + timedelta(seconds=60))
    await _quote(redis, clock.now)
    sc = _scalper(maker, redis, router, clock, rule, runner)
    broker.fake.alpaca.positions["AAPL"] = D("0.5")  # the broker refuses the sell of 1
    await sc.track()
    await _settle(maker, redis, qs, broker, clock.now)
    [first] = await _autox(maker, rule)
    assert first.status == OrderStatus.rejected
    broker.fake.alpaca.positions["AAPL"] = D(1)
    clock.now += timedelta(seconds=1)
    await sc.track()  # the tracker sees the refusal, the runner's Exit sends again
    await sc.track()
    orders = await _autox(maker, rule)
    assert [o.client_order_id for o in orders] == [f"smt-autox-{row.id}",
                                                   f"smt-autox-{row.id}-1"]
    await _settle(maker, redis, qs, broker, clock.now)
    clock.now += timedelta(seconds=1)
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "time"
    assert closed.exit_order_id == orders[1].id


async def test_session_exit_before_worker_net(maker, redis, qs, broker, router):
    start = datetime(2026, 11, 25, 20, 40, tzinfo=UTC)
    FROZEN["now"] = start
    rule = await _rule(maker, params={"max_hold_s": 3600})
    assert rule.day_close_minutes == 10
    row, runner = await _open(maker, redis, qs, broker, rule, now=start)
    clock = Clock(datetime(2026, 11, 25, 20, 47, 59, tzinfo=UTC))
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    assert await _autox(maker, rule) == []
    clock.now = datetime(2026, 11, 25, 20, 48, tzinfo=UTC)
    await _quote(redis, clock.now)
    await sc.track()
    assert len(await _autox(maker, rule)) == 1
    assert (await _trade(maker, row.id)).exit_reason == "session"
    broker.fill_time = clock.now + timedelta(milliseconds=300)
    await _settle(maker, redis, qs, broker, clock.now)
    clock.now += timedelta(seconds=1)
    await sc.track()
    assert (await _trade(maker, row.id)).status == CLOSED
    async with maker() as s:
        pos = await s.get(Position, (rule.portfolio_id, "AAPL"))
        assert pos is None or pos.qty == 0
    # 20:50Z: the worker's safety net finds no day quantity and sends nothing
    net = datetime(2026, 11, 25, 20, 50, tzinfo=UTC)
    await _quote(redis, net)
    await DayCloser(maker, qs, redis).tick(net)
    assert not [o for o in await _orders(maker, rule)
                if (o.client_order_id or "").startswith("smt-dayclose-")]


async def test_time_exit_never_refused_by_day_trade_checks(maker, redis, qs, broker, router):
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    at = row.filled_at + timedelta(seconds=60)
    async with maker() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == rule.portfolio_id)
                        .values(max_day_trades_5d=0))
        p = await s.get(Portfolio, rule.portfolio_id)
        link = await s.get(BrokerLink, p.broker_link_id)
        link.pdt_state, link.pdt_state_at = account(bp="0", at=at - timedelta(seconds=5))
        await s.commit()
    await _quote(redis, at)
    sc = _scalper(maker, redis, router, Clock(at), rule, runner)
    await sc.track()
    [exit_order] = await _autox(maker, rule)
    assert exit_order.status != OrderStatus.rejected and exit_order.reject_code is None
    assert exit_order.broker_status == BrokerState.TO_SEND


async def test_exits_survive_scalper_stop(maker, redis, qs, broker, router):
    rule = await _rule(maker)
    row, _ = await _open(maker, redis, qs, broker, rule)
    # the scalper stops here: the stop is kept by the order processor and BrokerSync
    sl = next(o for o in await _orders(maker, rule) if o.role == "stop_loss")
    later = NOW + timedelta(seconds=30)
    FROZEN["now"] = later
    low = sl.stop_price - D("0.05")
    broker.fake.price = low
    broker.fill_time = later + timedelta(milliseconds=200)
    q = await _quote(redis, later, bid=str(low), ask=str(low + D("0.1")))
    await OrderProcessor(maker, redis, qs, clock=Clock(later)).on_tick("AAPL", q)
    await _settle(maker, redis, qs, broker, later)
    assert (await _trade(maker, row.id)).status == OPEN  # nobody tracked it yet
    # restarted: the new scalper records the exit
    clock = Clock(later + timedelta(seconds=5))
    sc = _scalper(maker, redis, router, clock, rule, _runner(rule))
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "stop"
    assert closed.exit_expected == sl.stop_price and closed.exit_price == low
    assert closed.exit_order_id == sl.id


async def test_restart_rebuilds_open_trade(maker, redis, qs, broker, router):
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, _ = await _open(maker, redis, qs, broker, rule)
    clock = Clock(row.filled_at + timedelta(seconds=59))
    runner = _runner(rule)  # restarted: a new, flat runner
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    assert runner.state == "long" and runner.filled_at == row.filled_at
    assert runner.fill_price == row.fill_price
    assert await _autox(maker, rule) == []  # not before
    clock.now = row.filled_at + timedelta(seconds=60)
    await _quote(redis, clock.now)
    await sc.track()
    assert len(await _autox(maker, rule)) == 1
    clock.now += timedelta(seconds=1)
    await sc.track()
    # restarted again before the exit filled: not twice
    sc2 = _scalper(maker, redis, router, clock, rule, _runner(rule))
    await sc2.track()
    await sc2.track()
    assert len(await _autox(maker, rule)) == 1


async def test_stale_limit_entry_canceled(maker, redis, qs, broker):
    rule = await _rule(maker, params={"entry": "limit", "limit_offset_pct": "0.05"})
    assert rule.params["entry_timeout_s"] == 5
    runner = _runner(rule)
    broker.fake.price = D("201.00")  # the limit rests at the broker
    trade = await _send(maker, redis, qs, broker, rule, runner)
    assert runner.state == "pending"
    await _sync(maker, redis, qs, broker, NOW).tick()
    entry = next(o for o in await _orders(maker, rule) if o.role == "entry")
    assert entry.type == OrderType.limit and entry.broker_order_id is not None
    tracker = Tracker(maker, redis, qs, clock=Clock(NOW + timedelta(seconds=4)),
                      executor=Executor(maker, redis, qs, clock=Clock(NOW)))
    await tracker.step({rule.rule_id: runner})
    assert (await _trade(maker, trade.id)).status == "sent"
    tracker._clock = Clock(NOW + timedelta(seconds=5))
    await tracker.step({rule.rule_id: runner})
    entry = next(o for o in await _orders(maker, rule) if o.role == "entry")
    assert entry.broker_status == BrokerState.CANCEL_REQUESTED  # the cancel asked for
    await _settle(maker, redis, qs, broker, NOW + timedelta(seconds=5))
    await tracker.step({rule.rule_id: runner})
    row = await _trade(maker, trade.id)
    assert row.status == CANCELED
    assert runner.state == "flat"
    assert all(o.status == OrderStatus.canceled for o in await _orders(maker, rule))


async def test_broker_rejected_entry_frees_the_rule(maker, redis, qs, broker):
    """An entry the broker refuses ends the trade (`rejected`): the rule is not left
    busy, its runner is flat again."""
    rule = await _rule(maker)
    runner = _runner(rule)
    broker.fake.alpaca.cash = D(0)  # the fake refuses the buy
    trade = await _send(maker, redis, qs, broker, rule, runner)
    await _sync(maker, redis, qs, broker, NOW).tick()
    entry = next(o for o in await _orders(maker, rule) if o.role == "entry")
    assert entry.status == OrderStatus.rejected
    await Tracker(maker, redis, qs, clock=Clock(NOW + timedelta(seconds=1))).step(
        {rule.rule_id: runner})
    row = await _trade(maker, trade.id)
    assert row.status == "rejected" and row.error
    assert runner.state == "flat"
    later = await _quote(redis, NOW + timedelta(seconds=2))
    assert await Executor(maker, redis, qs, clock=Clock(later.ts)).enter(
        rule, runner, Entry(later.ts, later.ask, {}), later) is not None  # not busy


async def test_day_close_reason(maker, redis, qs, broker):
    rule = await _rule(maker)
    row, runner = await _open(maker, redis, qs, broker, rule)
    at = NOW + timedelta(seconds=30)
    await _quote(redis, at)
    async with maker() as s:  # what the worker's safety net sends
        await close_position(s, qs, rule.portfolio_id, "AAPL", row.qty, source="system",
                             agent_id=None, now=at, redis=redis,
                             client_order_id="smt-dayclose-20261125-AAPL")
        await s.commit()
        await dispatch_post_commit(s, redis)
    broker.fill_time = at + timedelta(milliseconds=500)
    await _settle(maker, redis, qs, broker, at)
    await Tracker(maker, redis, qs, clock=Clock(at + timedelta(seconds=1))).step(
        {rule.rule_id: runner})
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "day_close"
    assert closed.exit_price == D("200.00")


# --- the evening summary ------------------------------------------------------------------

async def test_evening_summary_once(maker, redis, qs, broker):
    rule = await _rule(maker)
    async with maker() as s:
        for i, net in enumerate((D("12.5"), D("-4.25"))):
            s.add(AutoTrade(
                rule_id=rule.rule_id, rule_version=rule.version,
                portfolio_id=rule.portfolio_id, symbol="AAPL", trigger_key=f"v1:{i}",
                trigger_at=NOW + timedelta(minutes=i), trigger_price=D(200),
                trigger_values={}, expected_price=D(200), status=CLOSED, qty=D(1),
                fill_price=D("200.02"), entry_slippage_bps=D(i + 1),
                ms_trigger_to_send=100 + i, ms_send_to_broker=150, ms_broker_to_fill=350,
                exit_reason="target" if net > 0 else "stop", fees=D("0.5"),
                fees_currency="USD", net_result=net, net_currency="EUR"))
        await s.commit()
    for reason in ("stale", "spread", "busy"):
        await count_blocked(redis, rule.rule_id, NOW, reason)

    async def outbox() -> list[TelegramOutbox]:
        async with maker() as s:
            return list(await s.scalars(select(TelegramOutbox).where(
                TelegramOutbox.text.like("Riepilogo regole automatiche%"))))

    early = datetime(2026, 11, 25, 21, 29, tzinfo=UTC)  # 22:29 in Rome
    assert await send_due_summaries(maker, redis, early, checked=set()) == 0
    assert await outbox() == []
    due = datetime(2026, 11, 25, 21, 31, tzinfo=UTC)
    assert await send_due_summaries(maker, redis, due, checked=set()) == 1
    [row] = await outbox()
    assert (row.source, row.category) == ("system", "system")
    assert "2 operazioni (1 in guadagno, 1 in perdita)" in row.text
    assert "risultato netto 8,25 EUR" in row.text
    assert "scatti bloccati 3 (1 prezzo vecchio, 1 forbice larga" in row.text
    assert "1 già in posizione" in row.text
    assert "solo IEX" in row.text
    assert await send_due_summaries(maker, redis, due, checked=set()) == 0
    assert len(await outbox()) == 1


# --- fix round 1: no second sell, partial fills ---------------------------------------------

async def _fractional(maker) -> None:
    async with maker() as s:
        (await s.get(Instrument, "AAPL")).lot_size = D("0.1")
        await s.commit()


async def test_time_exit_waits_for_handed_stop(maker, redis, qs, broker, router):
    """A stop triggered and handed to BrokerSync, the time exit due: no second sell; the
    trade closes as `stop`."""
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    sl = next(o for o in await _orders(maker, rule) if o.role == "stop_loss")
    at = row.filled_at + timedelta(seconds=60)
    FROZEN["now"] = at
    low = sl.stop_price - D("0.05")
    q = await _quote(redis, at, bid=str(low), ask=str(low + D("0.1")))
    await OrderProcessor(maker, redis, qs, clock=Clock(at)).on_tick("AAPL", q)
    handed = next(o for o in await _orders(maker, rule) if o.role == "stop_loss")
    assert handed.broker_status == BrokerState.TO_SEND  # on its way, not sent yet
    clock = Clock(at)
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    assert await _autox(maker, rule) == []
    broker.fake.price = low
    broker.fill_time = at + timedelta(milliseconds=300)
    await _settle(maker, redis, qs, broker, at)
    clock.now = at + timedelta(seconds=6)  # past the retry wait
    await sc.track()
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "stop"
    assert await _autox(maker, rule) == []


async def test_time_exit_waits_for_pending_day_close(maker, redis, qs, broker, router):
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    at = row.filled_at + timedelta(seconds=30)
    await _quote(redis, at)
    async with maker() as s:  # the worker's safety close, still waiting for BrokerSync
        await close_position(s, qs, rule.portfolio_id, "AAPL", row.qty, source="system",
                             agent_id=None, now=at, redis=redis,
                             client_order_id="smt-dayclose-20261125-AAPL")
        await s.commit()
        await dispatch_post_commit(s, redis)
    clock = Clock(row.filled_at + timedelta(seconds=60))
    await _quote(redis, clock.now)
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    assert await _autox(maker, rule) == []  # no second sell
    broker.fill_time = clock.now + timedelta(milliseconds=200)
    await _settle(maker, redis, qs, broker, clock.now)
    clock.now += timedelta(seconds=1)
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "day_close"
    assert await _autox(maker, rule) == []


async def test_day_close_skips_pending_rule_exit(maker, redis, qs, broker, router,
                                                monkeypatch):
    """The rule's session exit still `to_send` (BrokerSync down) at 20:50Z: the worker's
    safety net does not sell the same quantity again."""
    from smtrading.intraday import day_close

    start = datetime(2026, 11, 25, 20, 40, tzinfo=UTC)
    FROZEN["now"] = start
    rule = await _rule(maker, params={"max_hold_s": 3600})
    row, runner = await _open(maker, redis, qs, broker, rule, now=start)
    clock = Clock(datetime(2026, 11, 25, 20, 48, tzinfo=UTC))
    await _quote(redis, clock.now)
    await _scalper(maker, redis, router, clock, rule, runner).track()
    [pending] = await _autox(maker, rule)
    assert pending.broker_status == BrokerState.TO_SEND

    async def day_qty(session, portfolio_id, now):  # the day quantity bought today
        return {"AAPL": row.qty} if portfolio_id == rule.portfolio_id else {}

    monkeypatch.setattr(day_close, "day_quantities", day_qty)
    net = datetime(2026, 11, 25, 20, 50, tzinfo=UTC)
    FROZEN["now"] = net
    await _quote(redis, net)
    await DayCloser(maker, qs, redis).tick(net)
    assert not [o for o in await _orders(maker, rule)
                if (o.client_order_id or "").startswith("smt-dayclose-")]


async def test_partial_exit_replaced_for_the_rest(maker, redis, qs, broker, router):
    """The time exit fills 0.6 of 1, the rest canceled: the trade stays open, a new exit
    sells the 0.4 left, then the trade closes on both fills."""
    await _fractional(maker)
    rule = await _rule(maker, params={"max_hold_s": 60})
    row, runner = await _open(maker, redis, qs, broker, rule)
    clock = Clock(row.filled_at + timedelta(seconds=60))
    await _quote(redis, clock.now)
    sc = _scalper(maker, redis, router, clock, rule, runner)
    await sc.track()
    broker.fake.price, broker.partial_sell = D("200.40"), D("0.6")
    broker.fill_time = clock.now + timedelta(milliseconds=200)
    await _settle(maker, redis, qs, broker, clock.now)
    [first] = await _autox(maker, rule)
    assert first.filled_qty == D("0.6") and first.status == OrderStatus.canceled
    clock.now += timedelta(seconds=1)
    await sc.track()  # partly out: still open, the rest goes again
    assert (await _trade(maker, row.id)).status == OPEN
    assert runner.state == "long"
    orders = await _autox(maker, rule)
    assert [o.client_order_id for o in orders] == [f"smt-autox-{row.id}",
                                                   f"smt-autox-{row.id}-1"]
    assert orders[1].qty == D("0.4")
    broker.fake.price, broker.partial_sell = D("200.20"), None
    await _settle(maker, redis, qs, broker, clock.now)
    clock.now += timedelta(seconds=1)
    await sc.track()
    closed = await _trade(maker, row.id)
    assert closed.status == CLOSED and closed.exit_reason == "time"
    assert closed.exit_price == D("200.32")  # (0.6 × 200.40 + 0.4 × 200.20) / 1
    fees, [fee_ccy] = await _fees(maker, row.entry_order_id, *(o.id for o in orders))
    assert closed.fees == fees
    fee_rate = {"USD": D("0.9"), "EUR": D(1)}[fee_ccy]
    gross = D("0.6") * (D("200.40") - row.fill_price) + D("0.4") * (D("200.20")
                                                                    - row.fill_price)
    assert closed.net_result == gross * D("0.9") - fees * fee_rate
    assert runner.state == "flat"


async def test_partial_entry_fill(maker, redis, qs, broker):
    """A limit entry filled 0.4 of 1, the rest canceled: the trade opens on 0.4, its stop
    and target sized to it."""
    await _fractional(maker)
    rule = await _rule(maker, params={"entry": "limit", "limit_offset_pct": "0.05"})
    runner = _runner(rule)
    broker.fake.price = D("201.00")  # the limit rests
    trade = await _send(maker, redis, qs, broker, rule, runner)
    await _sync(maker, redis, qs, broker, NOW).tick()
    entry = next(o for o in await _orders(maker, rule) if o.role == "entry")
    o = broker.fake.alpaca.orders[entry.broker_order_id]
    o.update(filled_qty="0.4", filled_avg_price="200.05", status="canceled")
    broker.fake.alpaca.positions["AAPL"] = D("0.4")
    await _settle(maker, redis, qs, broker, NOW + timedelta(seconds=2))
    await Tracker(maker, redis, qs, clock=Clock(NOW + timedelta(seconds=3))).step(
        {rule.rule_id: runner})
    row = await _trade(maker, trade.id)
    assert row.status == OPEN and row.qty == D("0.4") and row.fill_price == D("200.05")
    # Alpaca reports no filled_at on an order canceled after a partial fill: local time
    assert row.fill_ts_source == "local" and row.filled_at is not None
    assert runner.state == "long"
    children = [x for x in await _orders(maker, rule) if x.role in ("take_profit",
                                                                    "stop_loss")]
    assert children and all(x.qty == D("0.4") for x in children)


async def test_tracker_without_executor_cancels_nothing(maker, redis, qs, broker):
    """A scalper with no executor (`executor_factory=None`): the Tracker records, it never
    cancels nor sends."""
    rule = await _rule(maker, params={"entry": "limit", "limit_offset_pct": "0.05"})
    runner = _runner(rule)
    broker.fake.price = D("201.00")
    await _send(maker, redis, qs, broker, rule, runner)
    await _sync(maker, redis, qs, broker, NOW).tick()
    tracker = Tracker(maker, redis, qs, clock=Clock(NOW + timedelta(seconds=30)))
    await tracker.step({rule.rule_id: runner})
    entry = next(o for o in await _orders(maker, rule) if o.role == "entry")
    assert entry.broker_status != BrokerState.CANCEL_REQUESTED
    assert Scalper(maker, redis, FakeQuoteSource(), executor_factory=None)._tracker._executor \
        is None
