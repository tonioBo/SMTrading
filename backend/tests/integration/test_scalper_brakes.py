"""Mode B brakes per rule and per portfolio: the caps checked before every entry (trades
per hour and per day, open positions: the entry blocked and counted `braked`, the rule
left active) and the brakes that stop after every closed trade (losses in a row, the
day's loss in the portfolio's currency and as a share of the day's opening equity): the
rule, or every active rule of the portfolio, «fermata dai freni» until resumed by hand,
one Telegram notice per rule, day and reason. The exits of an open trade go on.

Most tests write the closed trades directly (as the Tracker writes them) and call the
BrakeChecker; the exits test runs the real Executor, Tracker, BrokerSync and scalper on a
fake Alpaca. Names are made up."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.autorules import rules
from smtrading.autorules.brakes import BrakeChecker
from smtrading.autorules.executor import Executor
from smtrading.autorules.guards import BRAKED, REJECT_KEY, blocked_key
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import ActiveRule, active_rules
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.autorules.service import Scalper
from smtrading.autorules.tables import ACTIVE, CLOSED, OPEN, AutoRule, AutoTrade
from smtrading.autorules.tables import BRAKED as RULE_BRAKED
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import HEARTBEAT_KEY, BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import OPEN_STATUSES, Order, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import dispatch_post_commit
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY
from smtrading.marketdata.types import Bar, Quote
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)  # Wednesday, 11:00 New York: NYSE open
BY = "admin@example.com"
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}
NOTICE = "Regola automatica fermata dai freni"
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
    q.set_fx("USD", "EUR", D("0.9"))
    return q


@pytest.fixture
def qs(redis, router):
    return CacheQuoteSource(redis, router)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def account(at=NOW - timedelta(minutes=1)):
    return {"buying_power": "100000", "non_marginable_buying_power": "100000",
            "multiplier": "2", "equity": "100000", "trading_blocked": False,
            "account_blocked": False, "read_at": at.isoformat()}, at


async def _portfolio(sm, *, brakes=None, cash=100000, base="EUR", link=False):
    async with sm() as s:
        p = await create_portfolio(s, name=f"S-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(cash), base_currency=base, leverage=D(1))
        p.auto_rules_enabled = True
        p.auto_brakes = brakes or {}
        if link:
            lk = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper", dict(CFG))
            await link_portfolio(s, p.id, lk.id)
            lk.pdt_state, lk.pdt_state_at = account()
        await s.commit()
        return p


ACTIVATED = NOW - timedelta(days=2)  # before every trade of the tests


async def _rule(sm, portfolio, symbol="AAPL", *, brakes=None, params=None) -> ActiveRule:
    async with sm() as s:
        r = await rules.create_rule(s, portfolio_id=portfolio.id, symbol=symbol,
                                    model="breakout",
                                    params={"lookback_min": 20, **(params or {})},
                                    qty=D(1), notional=None, brakes=brakes, by=BY)
        await rules.activate(s, r.id, by=BY, now=ACTIVATED)
        await s.commit()
        [rule] = [a for a in await active_rules(s) if a.rule_id == r.id]
        return rule


async def _resume(sm, redis, rule, now) -> None:
    async with sm() as s:
        await rules.resume(s, rule.rule_id, by=BY, now=now)
        await s.commit()
        await dispatch_post_commit(s, redis)


async def _trade(sm, rule: ActiveRule, *, net=None, at=NOW, status=CLOSED,
                 currency="EUR") -> AutoTrade:
    """A trade of the rule triggered at `at`; closed one second later with `net`."""
    async with sm() as s:
        t = AutoTrade(rule_id=rule.rule_id, rule_version=rule.version,
                      portfolio_id=rule.portfolio_id, symbol=rule.symbol,
                      trigger_key=f"v{rule.version}:{uuid.uuid4().hex}", trigger_at=at,
                      trigger_price=D(200), trigger_values={}, expected_price=D(200),
                      status=status, qty=D(1), sent_at=at, filled_at=at, fill_price=D(200))
        if status == CLOSED:
            t.exit_filled_at = at + timedelta(seconds=1)
            t.net_result, t.net_currency = D(net), currency
            t.exit_reason = "stop" if t.net_result < 0 else "target"
        s.add(t)
        await s.commit()
        return t


async def _close(sm, checker, rule, net, *, at, currency="EUR") -> AutoTrade:
    t = await _trade(sm, rule, net=net, at=at, currency=currency)
    await checker.after_close(t, at + timedelta(seconds=1))
    return t


async def _status(sm, rule) -> tuple[str, str | None]:
    async with sm() as s:
        r = await s.get(AutoRule, rule.rule_id, populate_existing=True)
        return r.status, r.status_reason


async def _notices(sm, prefix=NOTICE) -> list[tuple]:
    async with sm() as s:
        rows = await s.scalars(select(TelegramOutbox).where(
            TelegramOutbox.text.startswith(prefix)).order_by(TelegramOutbox.id))
        return [(r.source, r.category, r.level, r.text) for r in rows]


async def _ready(redis, *symbols, now=NOW) -> None:
    await redis.set(STATUS_KEY, json.dumps({
        "streams": {"alpaca": {"state": CONNECTED, "symbols": list(symbols),
                               "bars": list(symbols)}},
        "polling": []}))
    await redis.set(HEARTBEAT_KEY, (now - timedelta(seconds=1)).isoformat())


async def _quote(redis, symbol="AAPL", ts=NOW, bid="199.90", ask="200.00") -> Quote:
    q = Quote(symbol=symbol, bid=D(bid), ask=D(ask), last=D(ask), ts=ts, provider="fake")
    await write_quote(redis, q, fetched_at=ts)
    return q


def _runner(rule: ActiveRule) -> RuleRunner:
    """A runner with a fresh minute bar (the one of the minute before NOW)."""
    runner = RuleRunner(rule.model, rule.params, market=rule.market,
                        tick_size=rule.tick_size,
                        session_exit_before=timedelta(minutes=rule.day_close_minutes + 2))
    runner.on_bar(Bar(ts=NOW - timedelta(minutes=1), open=D(199), high=D(200), low=D(198),
                      close=D(199), volume=D(100)))
    return runner


async def _orders(sm, rule) -> list[Order]:
    async with sm() as s:
        return list(await s.scalars(
            select(Order).where(Order.portfolio_id == rule.portfolio_id)
            .order_by(Order.created_at, Order.role)))


async def _counted(redis, rule, now=NOW) -> dict[str, int]:
    raw = await redis.hgetall(blocked_key(rule.rule_id, now))
    return {k.decode(): int(v) for k, v in raw.items()}


# --- the brakes that stop -----------------------------------------------------------

async def test_consecutive_losses_brake(sm, redis, qs):
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 3})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=30))
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=20))
    assert (await _status(sm, rule))[0] == ACTIVE
    third = await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=10))
    assert await _status(sm, rule) == (RULE_BRAKED, "3 perdite di fila")
    [(source, category, level, text)] = await _notices(sm)
    assert (source, category, level) == ("system", "system", "warning")
    assert text == (f"{NOTICE} · {p.name} · AAPL (breakout v1): 3 perdite di fila. "
                    "Riattivala a mano dal portale quando vuoi.")
    await checker.after_close(third, NOW)  # seen again: no second notice
    assert len(await _notices(sm)) == 1
    # the next trigger, from the scalper's older snapshot: no order
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), Entry(q.ts, q.ask, {}), q) is None
    assert await _orders(sm, rule) == []
    assert await _counted(redis, rule) == {BRAKED: 1}


async def test_win_resets_losing_streak(sm, redis, qs):
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 3})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    for i, net in enumerate(["-5", "-5", "3", "-5", "-5"]):
        await _close(sm, checker, rule, net, at=NOW - timedelta(minutes=50 - i))
    assert (await _status(sm, rule))[0] == ACTIVE
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=40))
    assert (await _status(sm, rule))[0] == RULE_BRAKED


async def test_yesterdays_losses_do_not_count(sm, redis, qs):
    """The day is the portfolio's local day (Europe/Rome: midnight is 23:00 UTC)."""
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 3})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    yesterday = datetime(2026, 11, 24, 22, 50, tzinfo=UTC)  # 23:50 in Rome
    await _trade(sm, rule, net="-5", at=yesterday - timedelta(minutes=5))
    await _trade(sm, rule, net="-5", at=yesterday)
    await _close(sm, checker, rule, "-5", at=datetime(2026, 11, 24, 23, 5, tzinfo=UTC))
    assert (await _status(sm, rule))[0] == ACTIVE


@pytest.mark.parametrize("stored", ["EUR", "USD"])
@pytest.mark.parametrize(("second", "braked"), [("41", True), ("39", False)])
async def test_daily_loss_in_portfolio_currency(sm, redis, qs, stored, second, braked):
    """EUR portfolio, USD→EUR 0,9, max_loss_day 90: losses of 60 $ and 41 $ are 90,90 €
    (braked), 60 $ and 39 $ are 89,10 € (not). The trade's result as the Tracker writes
    it (in EUR) or, should it be in another currency, converted at the same rate."""
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_loss_day": D(90), "max_consecutive_losses": None})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    rate = D("0.9") if stored == "EUR" else D(1)
    await _close(sm, checker, rule, -D(60) * rate, at=NOW - timedelta(minutes=20),
                 currency=stored)
    await _close(sm, checker, rule, -D(second) * rate, at=NOW - timedelta(minutes=10),
                 currency=stored)
    status, reason = await _status(sm, rule)
    if braked:
        assert status == RULE_BRAKED
        assert reason == "perdita del giorno 90,90 EUR (limite 90,00 EUR)"
        assert len(await _notices(sm)) == 1
    else:
        assert status == ACTIVE and await _notices(sm) == []


@pytest.mark.parametrize(("base", "limit"), [("EUR", D(300)), ("USD", D(375))])
async def test_daily_loss_pct(sm, redis, base, limit):
    """Opening equity 30.000 €, max_loss_day_pct 1: the brake at 300 € (375 $ for a USD
    portfolio at EUR→USD 1,25, the rate the portfolio uses)."""
    router = FakeQuoteSource()
    router.set_fx("EUR", "USD", D("1.25"))
    qs = CacheQuoteSource(redis, router)
    p = await _portfolio(sm, cash=30000, base=base)
    async with sm() as s:
        s.add(EquitySnapshot(portfolio_id=p.id, ts=NOW - timedelta(hours=10),
                             kind=SnapshotKind.day_open, local_date=NOW.date(),
                             equity_eur=D(30000), cash_eur=D(30000), exposure_eur=D(0),
                             margin_used_eur=D(0)))
        await s.commit()
    rule = await _rule(sm, p, brakes={"max_loss_day_pct": D(1), "max_consecutive_losses": None})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, rule, -(limit - D("0.01")), at=NOW - timedelta(minutes=20),
                 currency=base)
    assert (await _status(sm, rule))[0] == ACTIVE
    await _close(sm, checker, rule, "3", at=NOW - timedelta(minutes=15), currency=base)
    await _close(sm, checker, rule, "-3.01", at=NOW - timedelta(minutes=10), currency=base)
    status, reason = await _status(sm, rule)
    assert status == RULE_BRAKED
    assert reason.startswith("perdita del giorno") and "1% del patrimonio d'apertura" in reason


async def test_portfolio_brake_stops_all_rules(sm, redis, qs):
    p = await _portfolio(sm, brakes={"max_consecutive_losses": 3})
    one = await _rule(sm, p, "AAPL", brakes={"max_consecutive_losses": 5})
    two = await _rule(sm, p, "MSFT", brakes={"max_consecutive_losses": 5})
    other = await _rule(sm, await _portfolio(sm), "AAPL")
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, one, "-5", at=NOW - timedelta(minutes=30))
    await _close(sm, checker, two, "-5", at=NOW - timedelta(minutes=20))
    assert (await _status(sm, one))[0] == ACTIVE
    await _close(sm, checker, one, "-5", at=NOW - timedelta(minutes=10))
    for rule in (one, two):
        assert await _status(sm, rule) == (RULE_BRAKED, "3 perdite di fila nel portafoglio")
    assert (await _status(sm, other))[0] == ACTIVE
    notices = await _notices(sm)
    assert len(notices) == 2
    assert {n[3].split(" · ")[2].split(" ")[0] for n in notices} == {"AAPL", "MSFT"}


async def test_resume_then_day_loss_brakes_again_with_notice(sm, redis, qs):
    """Resumed by hand on a day already over its loss limit: the next close stops the
    rule again, and this trip is announced too."""
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_loss_day": D(10), "max_consecutive_losses": None})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, rule, "-12", at=NOW - timedelta(minutes=30))
    assert (await _status(sm, rule))[0] == RULE_BRAKED
    await _resume(sm, redis, rule, NOW - timedelta(minutes=20))
    await _close(sm, checker, rule, "-1", at=NOW - timedelta(minutes=10))
    assert await _status(sm, rule) == (
        RULE_BRAKED, "perdita del giorno 13,00 EUR (limite 10,00 EUR)")
    assert len(await _notices(sm)) == 2


async def test_resume_starts_a_new_losing_run(sm, redis, qs):
    p = await _portfolio(sm, brakes={"max_consecutive_losses": None})  # the rule's only
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 3})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    for minutes in (50, 45, 40):
        await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=minutes))
    assert (await _status(sm, rule))[0] == RULE_BRAKED
    await _resume(sm, redis, rule, NOW - timedelta(minutes=35))
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=30))
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=25))
    assert (await _status(sm, rule))[0] == ACTIVE  # 2 since the resume, not 5
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=20))
    assert await _status(sm, rule) == (RULE_BRAKED, "3 perdite di fila")
    assert len(await _notices(sm)) == 2


async def test_resume_clears_refusal_cooldown(sm, redis, qs):
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 1})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=30))
    key = REJECT_KEY.format(rule_id=rule.rule_id)
    await redis.set(key, "RISK_LIMIT_EXCEEDED", ex=300)
    await _resume(sm, redis, rule, NOW)
    assert not await redis.exists(key)


async def test_pending_judgement_logs_and_notifies_once(sm, redis, qs, monkeypatch, caplog):
    p = await _portfolio(sm)
    rule = await _rule(sm, p)
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)

    async def boom(*a, **k):
        raise RuntimeError("database down")

    monkeypatch.setattr(checker, "_judge", boom)
    t = await _trade(sm, rule, net="-1", at=NOW - timedelta(minutes=5))
    caplog.set_level("INFO", logger="smtrading.autorules.brakes")
    await checker.after_close(t, NOW)
    for i in range(5):
        assert await checker.blocked(rule, NOW + timedelta(seconds=i)) == BRAKED

    def logged():
        return [r for r in caplog.records if "brakes not judged" in r.getMessage()]

    assert len(logged()) == 1
    [(source, category, level, text)] = await _notices(sm, "Regole automatiche")
    assert (source, category, level) == ("system", "system", "warning")
    assert text.startswith(f"Regole automatiche · {p.name}: ")
    assert await checker.blocked(rule, NOW + timedelta(minutes=11)) == BRAKED
    assert len(logged()) == 2  # again after the interval
    assert len(await _notices(sm, "Regole automatiche")) == 1


async def test_brake_check_failure_blocks_entries(sm, redis, qs, monkeypatch):
    """A failed brake check never lets an entry through (and never raises); a close whose
    brakes could not be judged blocks the portfolio's entries until it is."""
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 1})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)

    async def boom(*a, **k):
        raise RuntimeError("database down")

    monkeypatch.setattr(checker, "_caps", boom)
    assert await checker.blocked(rule, NOW) == BRAKED
    monkeypatch.undo()
    monkeypatch.setattr(checker, "_judge", boom)
    t = await _trade(sm, rule, net="3", at=NOW - timedelta(minutes=5))
    await checker.after_close(t, NOW)  # logged, never raised
    assert await checker.blocked(rule, NOW) == BRAKED
    monkeypatch.undo()
    assert await checker.blocked(rule, NOW) is None  # judged again, nothing tripped
    assert (await _status(sm, rule))[0] == ACTIVE


# --- the caps -------------------------------------------------------------------------

async def test_trade_caps_block_without_braking(sm, redis, qs):
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_trades_hour": 2})
    await _trade(sm, rule, net="1", at=NOW - timedelta(minutes=30))
    await _trade(sm, rule, net="1", at=NOW - timedelta(minutes=20))
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        rule, _runner(rule), Entry(q.ts, q.ask, {}), q) is None
    assert await _orders(sm, rule) == []
    assert await _counted(redis, rule) == {BRAKED: 1}
    assert (await _status(sm, rule))[0] == ACTIVE
    assert await _notices(sm) == []
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    assert await checker.blocked(rule, NOW + timedelta(minutes=29)) == BRAKED
    assert await checker.blocked(rule, NOW + timedelta(minutes=31)) is None


async def test_trades_per_day_cap_portfolio(sm, redis, qs):
    p = await _portfolio(sm, brakes={"max_trades_day": 2})
    one = await _rule(sm, p, "AAPL")
    two = await _rule(sm, p, "MSFT")
    await _trade(sm, one, net="1", at=NOW - timedelta(hours=5))
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    assert await checker.blocked(two, NOW) is None
    await _trade(sm, one, net="1", at=NOW - timedelta(hours=4))
    assert await checker.blocked(two, NOW) == BRAKED
    assert await checker.blocked(two, datetime(2026, 11, 25, 23, 1, tzinfo=UTC)) is None


async def test_max_open_positions_portfolio(sm, redis, qs):
    p = await _portfolio(sm, brakes={"max_open_positions": 1})
    one = await _rule(sm, p, "AAPL")
    two = await _rule(sm, p, "MSFT")
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    assert await checker.blocked(two, NOW) is None
    first = await _trade(sm, one, at=NOW - timedelta(minutes=1), status=OPEN)
    await _ready(redis, "MSFT")
    q = await _quote(redis, "MSFT")
    assert await Executor(sm, redis, qs, clock=Clock(NOW)).enter(
        two, _runner(two), Entry(q.ts, q.ask, {}), q) is None
    assert await _orders(sm, two) == []
    assert await _counted(redis, two) == {BRAKED: 1}
    assert (await _status(sm, two))[0] == ACTIVE
    async with sm() as s:
        row = await s.get(AutoTrade, first.id)
        row.status, row.net_result, row.net_currency = CLOSED, D(1), "EUR"
        row.exit_filled_at = NOW
        await s.commit()
    assert await checker.blocked(two, NOW) is None


async def test_resume_after_brake_is_manual(sm, redis, qs, router):
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 2})
    checker = BrakeChecker(sm, qs, clock=Clock(NOW), redis=redis)
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=20))
    await _close(sm, checker, rule, "-5", at=NOW - timedelta(minutes=10))
    assert (await _status(sm, rule))[0] == RULE_BRAKED
    tomorrow = NOW + timedelta(days=1)
    sc = Scalper(sm, redis, router, clock=Clock(tomorrow))
    await sc.reload()
    await sc.track()
    assert sc.runners("AAPL") == []
    assert await checker.blocked(rule, tomorrow) == BRAKED
    assert (await _status(sm, rule))[0] == RULE_BRAKED
    async with sm() as s:
        await rules.resume(s, rule.rule_id, by=BY, now=tomorrow)
        await s.commit()
    assert await _status(sm, rule) == (ACTIVE, None)
    assert await checker.blocked(rule, tomorrow) is None


# --- the exits go on -----------------------------------------------------------------

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
async def broker():
    fake = FakeLiveBroker(price=D("200.00"))
    fake.alpaca.cash = D(100000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake._handle)) as http:
        fake.http = http
        yield fake


def _sync(maker, redis, qs, broker, now) -> BrokerSync:
    return BrokerSync(maker, redis, qs, broker.http, clock=Clock(now), interval_s=30)


async def test_brake_keeps_exits(maker, redis, qs, router, broker):
    """A brake with a trade open: the bracket's stop and target stay, and the rule's time
    exit is still sent by the scalper."""
    p = await _portfolio(maker, link=True)
    rule = await _rule(maker, p, brakes={"max_consecutive_losses": 1},
                       params={"max_hold_s": 60})
    runner = _runner(rule)
    await _ready(redis, "AAPL")
    q = await _quote(redis)
    trade = await Executor(maker, redis, qs, clock=Clock(NOW)).enter(
        rule, runner, Entry(q.ts, q.ask, {}), q)
    assert trade is not None
    await _sync(maker, redis, qs, broker, NOW).tick()
    clock = Clock(NOW + timedelta(seconds=1))
    sc = Scalper(maker, redis, router, clock=clock)
    sc._rules[rule.rule_id] = (rule, runner)
    sc._index()
    await sc.track()
    assert runner.state == "long"
    runner.entered(clock.now, runner.fill_price)  # the fake gives no fill time: NOW + 1 s
    # an earlier losing trade of the rule closes now: the brake trips
    lost = await _trade(maker, rule, net="-5", at=NOW - timedelta(minutes=10))
    await sc._executor.brakes.after_close(lost, clock.now)
    assert (await _status(maker, rule))[0] == RULE_BRAKED
    await sc.reload()  # the rule is no longer active
    children = [o for o in await _orders(maker, rule) if o.role in ("take_profit",
                                                                    "stop_loss")]
    assert len(children) == 2 and all(o.status in OPEN_STATUSES for o in children)
    clock.now = NOW + timedelta(seconds=61)
    await _quote(redis, ts=clock.now)
    await sc.track()
    exits = [o for o in await _orders(maker, rule)
             if (o.client_order_id or "").startswith("smt-autox-")]
    assert len(exits) == 1 and exits[0].type == OrderType.market and exits[0].reduce_only
    async with maker() as s:
        row = await s.get(AutoTrade, trade.id)
        assert row.status == OPEN and row.exit_reason == "time"
        n = await s.scalar(select(func.count()).select_from(AutoTrade).where(
            AutoTrade.rule_id == rule.rule_id))
        assert n == 2
    # the exit fills: the trade closes and the kept runner goes
    await _sync(maker, redis, qs, broker, clock.now).tick()
    clock.now += timedelta(seconds=1)
    await sc.track()
    async with maker() as s:
        assert (await s.get(AutoTrade, trade.id)).status == CLOSED
    assert runner.state == "flat" and sc._draining == {}


async def test_scalper_judges_brakes_on_close(sm, redis, qs, router, monkeypatch):
    """The trades the Tracker closes at a step have their brakes judged by the
    scalper."""
    p = await _portfolio(sm)
    rule = await _rule(sm, p, brakes={"max_consecutive_losses": 1})
    lost = await _trade(sm, rule, net="-5", at=NOW - timedelta(minutes=10))
    sc = Scalper(sm, redis, router, clock=Clock(NOW))

    async def step(runners):
        sc._tracker.closed = [lost]

    monkeypatch.setattr(sc._tracker, "step", step)
    await sc.track()
    assert await _status(sm, rule) == (RULE_BRAKED, "1 perdita di fila")
