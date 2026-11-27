"""Mode B entries: the guards before a send (price age, spread, open market, stream,
BrokerSync alive, the rule already busy) and the bracket entry through the existing
order path (risk checks included, day_trade for equities), one per trigger, BrokerSync
woken after the commit. Quotes from the Redis cache (`CacheQuoteSource`), exchange
rates from a FakeQuoteSource under it; an alpaca_paper link on a fake broker. Names are
made up."""
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules import rules
from smtrading.autorules.executor import Executor
from smtrading.autorules.guards import blocked_key
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import ActiveRule, active_rules
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.autorules.tables import OPEN, REJECTED, SENT, AutoTrade
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import HEARTBEAT_KEY, WAKE_CHANNEL, BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderStatus, OrderType, TimeInForce
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.instruments.models import Instrument
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY
from smtrading.marketdata.types import Bar, Quote
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)  # Wednesday, 11:00 New York: NYSE open
THANKSGIVING = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
BY = "admin@example.com"
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


@pytest.fixture(autouse=True)
def _frozen_risk_clock(monkeypatch):
    """The risk check reads the wall clock for a quote's age; the quotes are stamped
    around NOW, so the check sees NOW too."""
    import smtrading.risk.checks as risk_checks

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(risk_checks, "datetime", _Frozen)


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def sm(migrated_db):
    """Sessions on one connection inside a rolled-back transaction (savepoints)."""
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


@pytest.fixture
def router():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    return q


@pytest.fixture
def qs(redis, router):
    return CacheQuoteSource(redis, router)


def account(bp="100000", at=NOW - timedelta(minutes=1)):
    return {"buying_power": bp, "non_marginable_buying_power": bp, "multiplier": "2",
            "equity": "100000", "trading_blocked": False, "account_blocked": False,
            "read_at": at.isoformat()}, at


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _rule(sm, symbol="AAPL", *, qty=D(1), notional=None, params=None, cash=100000,
                link_kind="alpaca_paper", acct=None, **pf) -> ActiveRule:
    """An active rule on a portfolio with `auto_rules_enabled` linked to `link_kind`."""
    async with sm() as s:
        p = await create_portfolio(s, name=f"S-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(cash), leverage=D(1))
        p.auto_rules_enabled = True
        for k, v in pf.items():
            setattr(p, k, v)
        if link_kind == "alpaca_paper":
            link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper",
                                     dict(CFG))
            await link_portfolio(s, p.id, link.id)
        else:  # a real-money link, set directly (its linking needs TOTP)
            link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=link_kind)
            s.add(link)
            await s.flush()
            p.broker_link_id = link.id
        link.pdt_state, link.pdt_state_at = acct or account()
        await s.flush()
        r = await rules.create_rule(s, portfolio_id=p.id, symbol=symbol, model="breakout",
                                    params={"lookback_min": 20, **(params or {})},
                                    qty=qty, notional=notional, by=BY)
        await rules.activate(s, r.id, by=BY, now=NOW)
        await s.commit()
        [rule] = [a for a in await active_rules(s) if a.rule_id == r.id]
        return rule


def _runner(rule: ActiveRule, last_bar: datetime | None = NOW - timedelta(minutes=1)
            ) -> RuleRunner:
    """A runner whose newest minute bar starts at `last_bar` (None: no bar)."""
    runner = RuleRunner(rule.model, rule.params, market=rule.market,
                        tick_size=rule.tick_size, session_exit_before=timedelta(minutes=12))
    if last_bar is not None:
        runner.on_bar(Bar(ts=last_bar, open=D(199), high=D(200), low=D(198), close=D(199),
                          volume=D(100)))
    return runner


async def _ready(redis, *symbols, last_pass=NOW - timedelta(seconds=1)) -> None:
    """The symbols on a connected stream, BrokerSync's last pass at `last_pass`."""
    await redis.set(STATUS_KEY, json.dumps({
        "streams": {"alpaca": {"state": CONNECTED, "symbols": list(symbols),
                               "bars": list(symbols)}},
        "polling": []}))
    if last_pass is not None:
        await redis.set(HEARTBEAT_KEY, last_pass.isoformat())


async def _quote(redis, symbol="AAPL", bid="199.90", ask="200.00", ts=NOW, *,
                 suspect=None) -> Quote:
    q = Quote(symbol=symbol, bid=D(bid), ask=D(ask), last=D(ask), ts=ts, provider="fake",
              suspect=suspect)
    await write_quote(redis, q, fetched_at=ts)
    return q


def _entry(q: Quote) -> Entry:
    return Entry(q.ts, q.ask, {"high": q.ask - D("0.05")})


async def _orders(sm, rule: ActiveRule) -> list[Order]:
    async with sm() as s:
        return list(await s.scalars(
            select(Order).where(Order.portfolio_id == rule.portfolio_id)
            .order_by(Order.created_at, Order.role)))


async def _trades(sm, rule: ActiveRule) -> list[AutoTrade]:
    async with sm() as s:
        return list(await s.scalars(
            select(AutoTrade).where(AutoTrade.rule_id == rule.rule_id).order_by(AutoTrade.id)))


async def _blocked(redis, rule: ActiveRule, now=NOW) -> dict[str, int]:
    raw = await redis.hgetall(blocked_key(rule.rule_id, now))
    return {k.decode(): int(v) for k, v in raw.items()}


async def _wakes(redis):
    sub = redis.pubsub()
    await sub.subscribe(WAKE_CHANNEL)
    await sub.get_message(timeout=1)  # the subscribe confirmation
    return sub


async def _next_wake(sub, timeout=0.5) -> str | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = await sub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if m is not None:
            return m["data"].decode()
    return None


# --- the guards ------------------------------------------------------------------------

async def _open_trade(sm, rule: ActiveRule) -> None:
    async with sm() as s:
        s.add(AutoTrade(rule_id=rule.rule_id, rule_version=rule.version,
                        portfolio_id=rule.portfolio_id, symbol=rule.symbol,
                        trigger_key="v1:1", trigger_at=NOW - timedelta(minutes=1),
                        trigger_price=D(199), trigger_values={}, expected_price=D(199),
                        status=OPEN))
        await s.commit()


@pytest.mark.parametrize("case", ["stale", "spread", "closed", "suspect", "busy", "future"])
async def test_guards_block_entry(sm, redis, qs, case):
    rule = await _rule(sm)
    now = THANKSGIVING if case == "closed" else NOW
    await _ready(redis, "AAPL", last_pass=now - timedelta(seconds=1))
    if case == "stale":
        q = await _quote(redis, ts=now - timedelta(seconds=2, milliseconds=100))
    elif case == "spread":
        q = await _quote(redis, bid="99.945", ask="100.055")  # 0.11 %
    elif case == "suspect":
        q = await _quote(redis, suspect="jump")
    elif case == "future":  # stamped 1.1 s ahead of our clock: skew, not trusted
        q = await _quote(redis, ts=now + timedelta(seconds=1, milliseconds=100))
    else:
        q = await _quote(redis, ts=now)
    if case == "busy":
        await _open_trade(sm, rule)
    assert rule.params["max_age_s"] == D(2) and rule.params["max_spread_pct"] == D("0.10")
    trades_before = len(await _trades(sm, rule))
    runner = _runner(rule)
    out = await Executor(sm, redis, qs, clock=Clock(now)).enter(rule, runner, _entry(q), q)
    assert out is None
    assert await _orders(sm, rule) == []
    assert len(await _trades(sm, rule)) == trades_before
    assert runner.state == "flat"
    reason = "stale" if case in ("suspect", "future") else case
    assert await _blocked(redis, rule, now) == {reason: 1}
    ttl = await redis.ttl(blocked_key(rule.rule_id, now))
    assert 3 * 24 * 3600 - 10 < ttl <= 3 * 24 * 3600


async def test_quote_just_young_enough_passes(sm, redis, qs):
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis, ts=NOW - timedelta(seconds=2))  # exactly max_age_s: allowed
    trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q)
    assert trade is not None and trade.status == SENT


async def test_quote_slightly_ahead_passes(sm, redis, qs):
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis, ts=NOW + timedelta(seconds=1))  # within the 1 s skew bound
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q) is not None


async def test_no_entry_when_symbol_polled(sm, redis, qs):
    rule = await _rule(sm)
    await redis.set(HEARTBEAT_KEY, (NOW - timedelta(seconds=1)).isoformat())
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    await redis.set(STATUS_KEY, json.dumps({"streams": {}, "polling": ["AAPL"]}))
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is None
    await redis.delete(STATUS_KEY)  # no status at all (worker down)
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is None
    assert await _blocked(redis, rule) == {"no_stream": 2}
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []


async def test_no_entry_when_brokersync_silent(sm, redis, qs):
    rule = await _rule(sm)
    await _ready(redis, "AAPL", last_pass=NOW - timedelta(seconds=11))
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is None
    await redis.delete(HEARTBEAT_KEY)
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is None
    assert await _blocked(redis, rule) == {"broker_down": 2}
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []


@pytest.mark.parametrize("last_bar", [NOW - timedelta(minutes=4, seconds=1), None])
async def test_no_entry_on_stale_bars(sm, redis, qs, last_bar):
    """Newest bar closed more than 3 minutes ago (or none) with the market open: the
    indicators are frozen, no entry (fail closed when both stream and REST bars fail)."""
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    assert await ex.enter(rule, _runner(rule, last_bar), _entry(q), q) is None
    assert await _blocked(redis, rule) == {"bars_stale": 1}
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []
    # closed exactly 3 minutes ago: still fresh
    assert await ex.enter(rule, _runner(rule, NOW - timedelta(minutes=4)), _entry(q),
                          q) is not None


# --- the entry -------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,bid,ask,day_trade,tif", [
    ("AAPL", "199.90", "200.00", True, TimeInForce.day),
    ("BTC-USD", "999.90", "1000.00", False, TimeInForce.gtc),
])
async def test_entry_places_bracket(sm, redis, qs, symbol, bid, ask, day_trade, tif):
    rule = await _rule(sm, symbol)
    await _ready(redis, symbol)
    q = await _quote(redis, symbol, bid, ask)
    runner = _runner(rule)
    sub = await _wakes(redis)
    try:
        trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
            rule, runner, _entry(q), q)
        woken = await _next_wake(sub)
    finally:
        await sub.aclose()
    assert trade is not None and trade.status == SENT
    by_role = {o.role: o for o in await _orders(sm, rule)}
    entry, tp, sl = by_role["entry"], by_role["take_profit"], by_role["stop_loss"]
    assert len(by_role) == 3
    assert (entry.role, entry.type, entry.side.value, entry.qty) == ("entry", OrderType.market,
                                                                      "buy", D(1))
    assert entry.source == "system" and entry.agent_id is None
    assert entry.day_trade is day_trade and entry.tif == tif
    assert entry.auto_rule_id == rule.rule_id
    assert entry.client_order_id.startswith(f"smt-auto-{rule.rule_id.hex[:12]}-")
    stop, target = runner.levels(D(ask))
    assert (tp.role, tp.status, tp.limit_price) == ("take_profit", OrderStatus.held, target)
    assert (sl.role, sl.status, sl.stop_price) == ("stop_loss", OrderStatus.held, stop)
    assert tp.parent_id == sl.parent_id == entry.id
    assert tp.auto_rule_id == sl.auto_rule_id == rule.rule_id  # the rule's exits
    [row] = await _trades(sm, rule)
    assert row.status == SENT and row.expected_price == D(ask)
    assert row.entry_order_id == entry.id and row.qty == D(1)
    assert row.trigger_key == f"v{rule.version}:{int(q.ts.timestamp() * 1_000_000)}"
    assert (row.trigger_price, row.trigger_bid, row.trigger_ask) == (D(ask), D(bid), D(ask))
    assert row.trigger_values == {"high": str(D(ask) - D("0.05"))}
    assert row.sent_at == NOW and row.ms_trigger_to_send == 0
    assert woken == str(entry.id)
    assert runner.state == "pending"


async def test_notional_sized_to_the_lot(sm, redis, qs):
    rule = await _rule(sm, qty=None, notional=D(1000))
    async with sm() as s:
        lot = (await s.get(Instrument, "AAPL")).lot_size
    await _ready(redis, "AAPL")
    q = await _quote(redis, bid="299.90", ask="300.00")
    trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q)
    expected = (D(1000) / D(300) / lot).to_integral_value(rounding="ROUND_FLOOR") * lot
    assert trade.qty == expected and (await _orders(sm, rule))[0].qty == expected


async def test_notional_below_one_lot_no_order(sm, redis, qs):
    async with sm() as s:
        (await s.get(Instrument, "AAPL")).lot_size = D(1)
        await s.commit()
    rule = await _rule(sm, qty=None, notional=D(150))
    await _ready(redis, "AAPL")
    q = await _quote(redis)  # 200 $ a share
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q) is None
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []
    assert await _blocked(redis, rule) == {"size": 1}


async def test_same_trigger_twice_one_order(sm, redis, qs):
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    runner = _runner(rule)
    assert await ex.enter(rule, runner, _entry(q), q) is not None
    assert await ex.enter(rule, runner, _entry(q), q) is None
    assert len(await _orders(sm, rule)) == 3  # one entry and its two exits
    assert len(await _trades(sm, rule)) == 1


async def test_restart_after_commit_no_second_order(sm, redis, qs, monkeypatch):
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    first = _runner(rule)
    monkeypatch.setattr(first, "sent", lambda ts: (_ for _ in ()).throw(SystemExit))
    with pytest.raises(SystemExit):  # the process dies after the commit
        await Executor(sm, redis, qs, clock=Clock(NOW)).enter(rule, first, _entry(q), q)
    assert len(await _trades(sm, rule)) == 1
    ex, runner = Executor(sm, redis, qs, clock=Clock(NOW)), _runner(rule)  # restarted
    assert await ex.enter(rule, runner, _entry(q), q) is None
    later = await _quote(redis, ts=NOW + timedelta(seconds=1))
    ex2 = Executor(sm, redis, qs, clock=Clock(NOW + timedelta(seconds=1)))
    assert await ex2.enter(rule, runner, _entry(later), later) is None
    assert len(await _orders(sm, rule)) == 3 and len(await _trades(sm, rule)) == 1
    assert (await _blocked(redis, rule))["busy"] == 2


async def test_trigger_key_unique_even_when_not_busy(sm, redis, qs):
    """The unique (rule, trigger) key alone stops a trigger already worked: the trade and
    its orders closed (so the rule is not busy), the same Entry sends nothing; a new one
    does."""
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is not None
    async with sm() as s:
        await s.execute(update(AutoTrade).where(AutoTrade.rule_id == rule.rule_id)
                        .values(status="canceled"))
        await s.execute(update(Order).where(Order.portfolio_id == rule.portfolio_id)
                        .values(status=OrderStatus.canceled))
        await s.commit()
    assert await ex.enter(rule, _runner(rule), _entry(q), q) is None
    assert len(await _trades(sm, rule)) == 1 and len(await _orders(sm, rule)) == 3
    assert await _blocked(redis, rule) == {}
    later = await _quote(redis, ts=NOW + timedelta(seconds=1))
    ex2 = Executor(sm, redis, qs, clock=Clock(NOW + timedelta(seconds=1)))
    assert await ex2.enter(rule, _runner(rule), _entry(later), later) is not None
    assert len(await _trades(sm, rule)) == 2


async def test_new_version_starts_flat_still_busy(sm, redis, qs):
    """A runner replaced by a new version starts flat: the open trade of the rule (any
    version) still makes it busy."""
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q) is not None
    async with sm() as s:
        await rules.pause(s, rule.rule_id, by=BY)
        await rules.update_rule(s, rule.rule_id, params={"lookback_min": 10}, by=BY)
        await rules.resume(s, rule.rule_id, by=BY, now=NOW)
        await s.commit()
        [v2] = [a for a in await active_rules(s) if a.rule_id == rule.rule_id]
    assert v2.version == 2
    later = await _quote(redis, ts=NOW + timedelta(seconds=1))
    assert await Executor(sm, redis, qs, clock=Clock(NOW + timedelta(seconds=1))).enter(
        v2, _runner(v2), _entry(later), later) is None
    assert await _blocked(redis, rule) == {"busy": 1}
    assert len(await _trades(sm, rule)) == 1


async def _rejected(sm, redis, qs, rule, q=None) -> AutoTrade:
    q = q or await _quote(redis)
    runner = _runner(rule)
    sub = await _wakes(redis)
    try:
        trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
            rule, runner, _entry(q), q)
        assert await _next_wake(sub, timeout=0.2) is None
    finally:
        await sub.aclose()
    assert trade is not None and trade.status == REJECTED
    assert trade.sent_at is None and runner.state == "flat"
    [row] = await _trades(sm, rule)
    assert row.status == REJECTED and row.error == trade.error
    entries = [o for o in await _orders(sm, rule) if o.role == "entry"]
    assert all(o.status == OrderStatus.rejected for o in entries)
    assert not [o for o in await _orders(sm, rule) if o.status in (OrderStatus.held,
                                                                   OrderStatus.pending)]
    return row


async def test_risk_rejection_recorded_kill_switch(sm, redis, qs):
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    async with sm() as s:
        await set_kill_switch(s, True)
        await s.commit()
    row = await _rejected(sm, redis, qs, rule)
    assert row.error.startswith("KILL_SWITCH_ACTIVE")


async def test_risk_rejection_recorded_day_trade_cap(sm, redis, qs):
    rule = await _rule(sm, max_day_trades_5d=0)
    await _ready(redis, "AAPL")
    row = await _rejected(sm, redis, qs, rule)
    assert row.error.startswith("DAY_TRADE_LIMIT")


async def test_risk_rejection_recorded_broker_buying_power(sm, redis, qs):
    rule = await _rule(sm, qty=D(5), acct=account(bp="500"))  # 1,000 $ against 500
    await _ready(redis, "AAPL")
    row = await _rejected(sm, redis, qs, rule)
    assert row.error.startswith("INSUFFICIENT_BUYING_POWER")
    assert "broker_buying_power" in row.error


async def test_risk_rejection_recorded_live_day_leverage(sm, redis, qs, monkeypatch):
    from smtrading.risk import checks

    async def no_live_checks(*a, **k):  # arming, cash cap, fuse: not under test here
        return None

    monkeypatch.setattr(checks, "live_pre_trade", no_live_checks)
    # 10,000 EUR of equity, leverage 2 written; 93 x 200 $ = 14,880 EUR: over 1x
    rule = await _rule(sm, qty=D(93), cash=10000, link_kind="alpaca_live")
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == rule.portfolio_id)
                        .values(leverage=D(2)))
        await s.commit()
    await _ready(redis, "AAPL")
    row = await _rejected(sm, redis, qs, rule)
    assert row.error.startswith("INSUFFICIENT_BUYING_POWER")
    assert "day_trade_leverage" in row.error


async def test_limit_entry_price_rounded_up(sm, redis, qs):
    async with sm() as s:
        (await s.get(Instrument, "AAPL")).tick_size = D("0.01")
        await s.commit()
    rule = await _rule(sm, params={"entry": "limit", "limit_offset_pct": "0.05"})
    assert rule.tick_size == D("0.01")
    await _ready(redis, "AAPL")
    q = await _quote(redis, bid="100.003", ask="100.013")
    trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(q), q)
    assert trade is not None and trade.expected_price == D("100.013")
    entry = (await _orders(sm, rule))[0]
    assert entry.type == OrderType.limit and entry.limit_price == D("100.07")


async def test_executor_never_raises(sm, redis, qs, monkeypatch):
    from smtrading.autorules import executor as executor_module

    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_module, "place_bracket_order", broken)
    runner = _runner(rule)
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, runner, _entry(q), q) is None
    assert runner.state == "flat" and await _trades(sm, rule) == []


async def test_scalper_wires_the_executor(sm, redis, qs):
    from smtrading.autorules.service import Scalper

    scalper = Scalper(sm, redis, FakeQuoteSource(), clock=lambda: NOW)
    assert isinstance(scalper._executor, Executor)


# --- with the real BrokerSync ------------------------------------------------------------

@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    m = async_sessionmaker(engine, expire_on_commit=False)
    async with m() as s:
        await run_seed(s)
        await s.commit()
    yield m
    await engine.dispose()


async def test_trigger_to_send_under_one_second(maker, redis, qs):
    fake = FakeLiveBroker(price=D("200.00"))
    fake.alpaca.cash = D(100000)
    posted: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/orders":
            posted.append(time.monotonic())
        return fake._handle(request)

    rule = await _rule(maker)
    await _ready(redis, "AAPL", last_pass=None)
    q = await _quote(redis)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sync = BrokerSync(maker, redis, qs, http, clock=Clock(NOW), interval_s=30)
        task = asyncio.create_task(sync.run())
        try:
            for _ in range(200):  # the first pass done and the wake channel listened to
                [(_, n)] = await redis.pubsub_numsub(WAKE_CHANNEL)
                if n and await redis.get(HEARTBEAT_KEY):
                    break
                await asyncio.sleep(0.02)
            await redis.set(HEARTBEAT_KEY, (NOW - timedelta(seconds=1)).isoformat())
            t0 = time.monotonic()
            trade = await Executor(maker, redis, qs, clock=Clock(NOW)).enter(
                rule, _runner(rule), _entry(q), q)
            assert trade is not None and trade.status == SENT
            for _ in range(100):
                if posted:
                    break
                await asyncio.sleep(0.01)
            assert posted and posted[0] - t0 < 1.0
            await asyncio.sleep(0.2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    async with maker() as s:
        row = await s.scalar(select(AutoTrade).where(AutoTrade.rule_id == rule.rule_id))
        assert row.ms_trigger_to_send is not None and row.ms_trigger_to_send < 1000
    assert len(posted) == 1  # sent once, by the wake (the next full pass is 30 s away)


# --- fix round 1 ---------------------------------------------------------------------------

async def test_rejection_cools_down(sm, redis, qs):
    """A risk refusal is stored once: the next quotes meeting the same condition add no
    rows (counted as `rejected`) until the cooldown ends."""
    from smtrading.autorules import executor as executor_module

    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    async with sm() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await _rejected(sm, redis, qs, rule)
    runner = _runner(rule)
    for i in (1, 2):
        later = await _quote(redis, ts=NOW + timedelta(seconds=i))
        assert await Executor(sm, redis, qs, clock=Clock(NOW + timedelta(seconds=i))).enter(
            rule, runner, _entry(later), later) is None
    assert len(await _trades(sm, rule)) == 1
    assert len([o for o in await _orders(sm, rule) if o.role == "entry"]) == 1
    assert await _blocked(redis, rule) == {"rejected": 2}
    ttl = await redis.ttl(executor_module.REJECT_KEY.format(rule_id=rule.rule_id))
    assert 0 < ttl <= executor_module.REJECT_COOLDOWN_S


@pytest.mark.parametrize("change", ["switch_off", "paused", "new_version"])
async def test_no_entry_when_rule_changed_after_snapshot(sm, redis, qs, change):
    rule = await _rule(sm)  # the scalper's snapshot
    async with sm() as s:
        if change == "switch_off":
            p = await s.get(Portfolio, rule.portfolio_id)
            p.auto_rules_enabled = False
        else:
            await rules.pause(s, rule.rule_id, by=BY)
            if change == "new_version":
                await rules.update_rule(s, rule.rule_id, params={"lookback_min": 10}, by=BY)
                await rules.resume(s, rule.rule_id, by=BY, now=NOW)
        await s.commit()
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    runner = _runner(rule)
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, runner, _entry(q), q) is None
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []
    assert await _blocked(redis, rule) == {"inactive": 1}
    assert runner.state == "flat"


async def test_automatic_order_refused_with_switch_off(sm, redis, qs):
    """Risk step 11 on its own: an order carrying an auto_rule_id on a portfolio whose
    switch is off is refused (`auto_rule_inactive`); a reduce-only one is not judged."""
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.engine.models import OrderSide
    from smtrading.engine.service import PlaceOrderRequest, place_order

    rule = await _rule(sm)
    async with sm() as s:
        (await s.get(Portfolio, rule.portfolio_id)).auto_rules_enabled = False
        await s.commit()
    await _quote(redis)
    async with sm() as s:
        with pytest.raises(SMTError) as e:
            await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=rule.portfolio_id, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, tif=TimeInForce.day, qty=D(1), source="system",
                auto_rule_id=rule.rule_id), now=NOW)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert e.value.details["reason"] == "auto_rule_inactive"


async def test_stale_bars_block_logged_once_and_clear_logged(sm, redis, qs, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="smtrading.autorules.executor")
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    ex = Executor(sm, redis, qs, clock=Clock(NOW))
    stale = _runner(rule, NOW - timedelta(minutes=10))
    for _ in range(3):  # many quotes: one line
        assert await ex.enter(rule, stale, _entry(q), q) is None
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "stale" in r.getMessage()]
    assert len(warns) == 1
    assert await ex.enter(rule, _runner(rule, NOW - timedelta(minutes=4)), _entry(q), q)
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "fresh again" in r.getMessage()]
    assert len(infos) == 1


# --- the placement quote against the trigger's ---------------------------------------------

def _jpm_trigger() -> Quote:
    """The live drill's trigger (stop_pct 0.3, target_pct 0.5): expected = ask 323.38,
    stop 322.40 (322.41 live), target 325.00."""
    return Quote(symbol="AAPL", bid=D("323.10"), ask=D("323.38"), last=D("323.32"), ts=NOW,
                 provider="fake")


async def _tick(sm, symbol="AAPL") -> None:
    async with sm() as s:
        (await s.get(Instrument, symbol)).tick_size = D("0.01")
        await s.commit()


@pytest.mark.parametrize("cached_ask", ["325.10", "322.30", "324.00"])
async def test_price_moved_since_the_trigger_no_order(sm, redis, qs, cached_ask, caplog):
    """The cache the engine places on holds another ask than the trigger's, beyond half
    the stop distance: no order, no trade row, no cooldown, counted `price_moved`."""
    from smtrading.autorules import executor as executor_module

    await _tick(sm)
    rule = await _rule(sm, params={"stop_pct": "0.3", "target_pct": "0.5"})
    await _ready(redis, "AAPL")
    await _quote(redis, bid=str(D(cached_ask) - D("0.28")), ask=cached_ask)
    trigger = _jpm_trigger()
    runner = _runner(rule)
    assert runner.levels(trigger.ask) == (D("322.40"), D("325.00"))
    caplog.set_level("INFO")
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, runner, _entry(trigger), trigger) is None
    assert await _orders(sm, rule) == [] and await _trades(sm, rule) == []
    assert await _blocked(redis, rule) == {"price_moved": 1}
    assert not await redis.exists(executor_module.REJECT_KEY.format(rule_id=rule.rule_id))
    assert runner.state == "flat"
    assert any("price moved" in r.getMessage() and cached_ask in r.getMessage()
               and "323.38" in r.getMessage() for r in caplog.records)


async def test_placement_quote_close_to_trigger_levels_from_trigger(sm, redis, qs):
    """Within the tolerance the bracket goes, its levels from the trigger's price."""
    await _tick(sm)
    rule = await _rule(sm, params={"stop_pct": "0.3", "target_pct": "0.5"})
    await _ready(redis, "AAPL")
    await _quote(redis, bid="323.20", ask="323.50")
    trigger = _jpm_trigger()
    trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), _entry(trigger), trigger)
    assert trade is not None and trade.status == SENT
    assert trade.expected_price == D("323.38")
    by_role = {o.role: o for o in await _orders(sm, rule)}
    assert by_role["take_profit"].limit_price == D("325.00")
    assert by_role["stop_loss"].stop_price == D("322.40")


async def test_bracket_price_refusal_logged(sm, redis, qs, caplog):
    """A bracket-price refusal logs the reference, the expected price and the levels."""
    await _tick(sm)
    rule = await _rule(sm)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    runner = _runner(rule)
    runner.levels = lambda expected: (expected + D(1), expected + D(2))  # stop above
    caplog.set_level("INFO")
    trade = await Executor(sm, redis, qs, clock=Clock(NOW)).enter(rule, runner, _entry(q), q)
    assert trade is not None and trade.status == REJECTED
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reference 200.00" in text and "expected 200.00" in text
    assert "stop 201.00" in text and "target 202.00" in text
