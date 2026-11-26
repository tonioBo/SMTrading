"""The intraday worker job (`intraday.job.IntradayJob`): bar closes once per (timeframe,
market, close) across ticks and restarts, the day close for every portfolio in scope, the
account reads, the end-of-day summary; one query and nothing else when no portfolio has a
switch on. Market data, Paperclip and the broker are fakes; names are made up."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents import service as agents
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.instruments.market_hours import set_closures
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.intraday import job as job_mod
from smtrading.intraday.job import BAR_JOB, SUMMARY_JOB, IntradayJob
from smtrading.intraday.models import (
    CAPPED,
    FAILED,
    ISSUE_OPEN,
    LATE,
    NO_AGENT,
    PENDING,
    TELEGRAM,
    WOKEN,
    IntradayTrigger,
)
from smtrading.intraday.summary import day_summary_text
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import JobRun
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

DAY = date(2026, 12, 1)  # Tuesday, NYSE 14:30Z-21:00Z
BAR = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 12, 1, 21, 5, 30, tzinfo=UTC)
PARAMS = {"period": 2, "buy_below": 10, "sell_above": 70}


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


class Boom:
    """Router, Redis and HTTP client that fail on any use."""

    def __init__(self):
        self.used: list[str] = []

    def __getattr__(self, name):
        self.used.append(name)
        raise AssertionError(f"touched: {name}")


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Calls:
    """Fakes for evaluate_close, DayCloser and refresh_account_states."""

    def __init__(self, monkeypatch):
        self.evaluations: list[tuple] = []
        self.day_ticks: list[datetime] = []
        self.account_reads: list[datetime] = []
        calls = self

        async def fake_evaluate(session, router, redis, tf, market, bar_close, now, *,
                                exclude=frozenset()):
            calls.evaluations.append((tf.value, market, bar_close, exclude))
            return []

        class FakeDayCloser:
            def __init__(self, *args, **kwargs):
                pass

            async def tick(self, now):
                calls.day_ticks.append(now)

        async def fake_refresh(sessionmaker, http, now):
            calls.account_reads.append(now)
            return 0

        monkeypatch.setattr(job_mod, "evaluate_close", fake_evaluate)
        monkeypatch.setattr(job_mod, "DayCloser", FakeDayCloser)
        monkeypatch.setattr(job_mod, "refresh_account_states", fake_refresh)


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        for sym, market in (("JOBSPY", "NYSE"), ("JOBQQQ", "NASDAQ")):
            s.add(Instrument(symbol=sym, name=sym, asset_class=AssetClass.us_equity,
                             market=market, currency="USD"))
        await s.commit()
    yield sm, engine
    await engine.dispose()


async def _portfolio(sm, *, intraday=False, auto=False, cap=None) -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=f"Fittizio {uuid.uuid4().hex[:5]}",
                                   initial_cash=D(10000))
        p.intraday_enabled = intraday
        p.auto_rules_enabled = auto
        p.max_day_trades_5d = cap
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _strategy(sm, ref, symbols, portfolio_ids, timeframe="15m"):
    async with sm() as s:
        ceo, _ = await agents.create_agent(s, f"Agente {uuid.uuid4().hex[:5]}")
        s.add(Decision(
            agent_id=ceo.id, kind="strategy", summary=f"Strategia {ref}",
            details={"ref": ref, "status": "approvata", "strategy": "rsi_reversion",
                     "timeframe": timeframe, "params": PARAMS,
                     "portfolios": [str(p) for p in portfolio_ids]},
            symbols=list(symbols), portfolio_id=None))
        await s.commit()


async def _triggers(sm, pid, outcomes, bar=BAR):
    async with sm() as s:
        for i, outcome in enumerate(outcomes):
            s.add(IntradayTrigger(
                created_at=bar + timedelta(minutes=1), portfolio_id=pid,
                strategy_ref="S-1", strategy_name="Prova", symbol=f"SYM{i}", rule="entry",
                timeframe="15m", bar_close=bar, rule_text="RSI 2 sotto 10", outcome=outcome))
        await s.commit()


def _job(sm, now, router=None, redis=None, http=None, **kw) -> tuple[IntradayJob, Clock]:
    clock = Clock(now)
    return IntradayJob(sm, router or object(), redis or object(), http or object(),
                       clock=clock, tz="Europe/Rome", **kw), clock


async def _count(sm, model) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def test_idle_without_day_scope_portfolios(env):
    sm, engine = env
    await _portfolio(sm)  # both switches off
    await _strategy(sm, "J-1", ["JOBSPY"], [await _portfolio(sm)])
    router, redis, http = Boom(), Boom(), Boom()
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        job, _ = _job(sm, BAR + timedelta(seconds=65), router, redis, http)
        await job.tick()
        job._clock.t = AFTER_CLOSE
        await job.tick()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert len(statements) == 2  # one per tick
    assert all("FROM portfolios" in st for st in statements)
    assert router.used == redis.used == http.used == []
    assert await _count(sm, JobRun) == 0
    assert await _count(sm, IntradayTrigger) == 0
    assert await _count(sm, TelegramOutbox) == 0


async def test_auto_rules_only_runs_day_close_not_bars(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, auto=True)
    await _strategy(sm, "J-1", ["JOBSPY"], [pid])
    job, _ = _job(sm, BAR + timedelta(seconds=65))
    await job.tick()
    assert calls.day_ticks == [BAR + timedelta(seconds=65)]
    assert calls.evaluations == []


async def test_bar_processed_once(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _strategy(sm, "J-1", ["JOBSPY"], [pid])
    job, clock = _job(sm, datetime(2026, 12, 1, 15, 1, 5, tzinfo=UTC))
    await job.tick()
    clock.t = datetime(2026, 12, 1, 15, 1, 20, tzinfo=UTC)
    await job.tick()
    assert calls.evaluations == [("15m", "NYSE", BAR, frozenset())]
    async with sm() as s:
        keys = list(await s.scalars(select(JobRun.key).where(JobRun.job == BAR_JOB)))
    assert keys == [f"15m:NYSE:{BAR.isoformat()}"]


async def test_one_evaluation_per_market(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _strategy(sm, "J-1", ["JOBSPY", "JOBQQQ"], [pid])
    job, _ = _job(sm, BAR + timedelta(seconds=65))
    await job.tick()
    assert sorted((tf, m) for tf, m, *_ in calls.evaluations) == [
        ("15m", "NASDAQ"), ("15m", "NYSE")]


async def test_restart_does_not_reprocess(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _strategy(sm, "J-1", ["JOBSPY"], [pid])
    first, _ = _job(sm, BAR + timedelta(seconds=65))
    await first.tick()
    second, _ = _job(sm, BAR + timedelta(seconds=80))
    await second.tick()
    assert len(calls.evaluations) == 1


async def test_exclude_provider_feeds_the_evaluation(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _strategy(sm, "J-1", ["JOBSPY"], [pid])
    owned = frozenset({(pid, "JOBSPY")})

    async def provider(session):
        return owned

    job, _ = _job(sm, BAR + timedelta(seconds=65), exclude_provider=provider)
    await job.tick()
    assert [e[3] for e in calls.evaluations] == [owned]


async def test_account_reads_every_interval_while_open_and_once_after_close(env, monkeypatch):
    sm, _ = env
    calls = Calls(monkeypatch)
    await _portfolio(sm, auto=True)
    t0 = datetime(2026, 12, 1, 15, 0, 30, tzinfo=UTC)
    job, clock = _job(sm, t0)
    for t in (t0, t0 + timedelta(seconds=60),
              t0 + timedelta(seconds=job_mod.ACCOUNT_READ_INTERVAL_S),
              AFTER_CLOSE - timedelta(minutes=3), AFTER_CLOSE,
              AFTER_CLOSE + timedelta(minutes=20)):
        clock.t = t
        await job.tick()
    assert calls.account_reads == [
        t0, t0 + timedelta(seconds=job_mod.ACCOUNT_READ_INTERVAL_S),
        AFTER_CLOSE - timedelta(minutes=3)]


async def test_summary_once_after_close(env, monkeypatch):
    sm, _ = env
    Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _portfolio(sm, intraday=True)  # no triggers: no summary
    await _triggers(sm, pid, [WOKEN, CAPPED])
    job, clock = _job(sm, AFTER_CLOSE - timedelta(minutes=1))
    await job.tick()
    assert await _count(sm, TelegramOutbox) == 0  # before close + 5 min
    clock.t = AFTER_CLOSE
    await job.tick()
    await job.tick()
    again, _ = _job(sm, AFTER_CLOSE + timedelta(seconds=30))
    await again.tick()
    async with sm() as s:
        rows = list(await s.scalars(select(TelegramOutbox)))
        keys = list(await s.scalars(select(JobRun.key).where(JobRun.job == SUMMARY_JOB)))
    assert len(rows) == 1
    assert rows[0].text.startswith("Riepilogo di giornata")
    assert "2 scatti" in rows[0].text
    assert (rows[0].source, rows[0].category) == ("system", "system")
    assert keys == [f"{pid}:{DAY.isoformat()}"]


_COUNTS = re.compile(
    r"(\d+) scatti — (\d+) sveglie, (\d+) solo Telegram, (\d+) saltati per tetto, "
    r"(\d+) con compito già aperto, (\d+) senza agente, (\d+) dopo l'orario, "
    r"(\d+) non riusciti")


async def test_summary_counts_add_up(env):
    sm, _ = env
    pid = await _portfolio(sm, intraday=True)
    await _triggers(sm, pid, [WOKEN, TELEGRAM, CAPPED, ISSUE_OPEN, NO_AGENT, LATE, FAILED])
    await _triggers(sm, pid, [WOKEN], bar=BAR - timedelta(days=1))  # another session
    async with sm() as s:
        p = await s.get(Portfolio, pid)
        text = await day_summary_text(s, p, DAY)
    assert "7 scatti" in text and "1 solo Telegram" in text
    n, *parts = map(int, _COUNTS.search(text).groups())
    assert n == 7 and parts == [1] * 7 and sum(parts) == n
    assert f"· {p.name} · 01/12:" in text
    assert "operazioni di giornata 0" in text
    assert "PDT" not in text and "USA" not in text


async def test_summary_pending_counts_as_failed_and_shows_the_cap(env):
    sm, _ = env
    pid = await _portfolio(sm, intraday=True, cap=3)
    await _triggers(sm, pid, [PENDING, FAILED, WOKEN])
    async with sm() as s:
        p = await s.get(Portfolio, pid)
        text = await day_summary_text(s, p, DAY)
        assert await day_summary_text(s, p, DAY + timedelta(days=1)) is None
    n, *parts = map(int, _COUNTS.search(text).groups())
    assert n == 3 and parts[0] == 1 and parts[6] == 2
    assert "operazioni di giornata 0/3" in text


async def test_a_failing_step_is_logged_and_the_others_run(env, monkeypatch, caplog):
    sm, _ = env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True)
    await _strategy(sm, "J-1", ["JOBSPY"], [pid])

    async def broken(*args, **kwargs):
        raise RuntimeError("market data down")

    monkeypatch.setattr(job_mod, "evaluate_close", broken)
    job, _ = _job(sm, BAR + timedelta(seconds=65))
    await job.tick()
    assert calls.day_ticks and calls.account_reads
    assert "intraday bar" in caplog.text


async def test_run_logs_and_continues(env, monkeypatch, caplog):
    sm, _ = env
    job, _ = _job(sm, BAR, tick_s=0)
    seen = []

    async def tick():
        seen.append(1)
        if len(seen) == 1:
            raise RuntimeError("boom")
        if len(seen) == 3:
            raise KeyboardInterrupt  # stop the loop
    monkeypatch.setattr(job, "tick", tick)
    with pytest.raises(KeyboardInterrupt):
        await job.run()
    assert len(seen) == 3
    assert "intraday job failed" in caplog.text
