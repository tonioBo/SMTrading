"""The pre-open check (08:45, Monday to Friday): one Telegram message in the outbox
(category `system`), never in the activity feed; idempotent per day."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.brokers.live.monitor import ADVISORY_LOCK_KEY as MONITOR_LOCK
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.sync import ADVISORY_LOCK_KEY as SYNC_LOCK
from smtrading.common.db import make_engine
from smtrading.common.heartbeat import HEARTBEAT_KEY, write_heartbeat
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
from smtrading.jobs.runs import job_done
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk import halts
from smtrading.risk.models import AppSetting, RiskProfile
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.preopen import JOB, PreopenCheck
from smtrading.telegram.render import MAX_LEN, utf16_len

D = Decimal
TZ = "Europe/Rome"
ROME = ZoneInfo(TZ)
TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
MON = 15  # Monday 15 March 2027


def rome(day: int, h: int, m: int = 0, *, month: int = 3, year: int = 2027) -> datetime:
    return datetime(year, month, day, h, m, tzinfo=ROME).astimezone(UTC)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class NoQuotes(FakeQuoteSource):
    async def get_quotes(self, symbols, *, max_age_s=5):
        raise AssertionError("no quotes expected")


@pytest.fixture(autouse=True)
def _clear_warned():
    tg_config._warned.clear()
    yield
    tg_config._warned.clear()


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
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.commit()
    redis = Redis.from_url(redis_url)
    await redis.delete(HEARTBEAT_KEY)
    yield sm, engine, redis
    await redis.delete(HEARTBEAT_KEY)
    await redis.aclose()
    await engine.dispose()


async def _configure(sm) -> None:
    async with sm() as s:
        await save_config(s, TelegramConfig(bot_token=TOKEN, chat_id="-100123", enabled=True))
        await s.commit()


async def _setting(sm, key: str, value: dict) -> None:
    async with sm() as s:
        stmt = insert(AppSetting).values(key=key, value=value)
        await s.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                   set_={"value": value}))
        await s.commit()


async def _portfolio(sm, name: str, *, symbols: list[str] | None = None,
                     status: PortfolioStatus = PortfolioStatus.active) -> uuid.UUID:
    async with sm() as s:
        profile_id = None
        if symbols is not None:
            profile = RiskProfile(name=f"Profilo {name}", allowed_symbols=symbols)
            s.add(profile)
            await s.flush()
            profile_id = profile.id
        p = await create_portfolio(s, name=name, initial_cash=D(1000),
                                   risk_profile_id=profile_id)
        p.status = status
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _outbox(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _activity_count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(Activity))


class Locks:
    """Holds the BrokerSync and LiveMonitor advisory locks (as running loops do)."""

    def __init__(self, engine):
        self._engine = engine
        self._conn = None

    async def __aenter__(self):
        self._conn = await self._engine.connect()
        for key in (SYNC_LOCK, MONITOR_LOCK):
            assert await self._conn.scalar(text("select pg_try_advisory_lock(:k)"), {"k": key})
        await self._conn.commit()
        return self

    async def __aexit__(self, *exc):
        await self._conn.close()


def _quotes(now: datetime, *symbols: str) -> FakeQuoteSource:
    qs = FakeQuoteSource()
    for sym in symbols:
        qs.set_quote(sym, D("10"), D("10.1"), D("10.05"), ts=now - timedelta(hours=14))
    return qs


def _check(env, qs, clock) -> PreopenCheck:
    sm, _, redis = env
    return PreopenCheck(sm, qs, redis, clock=clock, tz=TZ)


async def test_ready_at_0845_once(env):
    sm, engine, redis = env
    await _configure(sm)
    pid = await _portfolio(sm, "Test 260 A", symbols=["MSFT", "AAPL"])
    async with sm() as s:
        s.add(Position(portfolio_id=pid, symbol="AAPL", qty=D(2), avg_price=D(100)))
        await s.commit()
    clock = Clock(rome(MON, 8, 44))
    qs = _quotes(clock.t, "AAPL", "MSFT")
    before = await _activity_count(sm)
    async with Locks(engine):
        await write_heartbeat(redis)
        await _check(env, qs, clock).tick()
        assert await _outbox(sm) == []
        clock.t = rome(MON, 8, 45)
        check = _check(env, qs, clock)
        await check.tick()
        await check.tick()
        clock.t = rome(MON, 9)
        await _check(env, qs, clock).tick()  # a restart: a new instance
    [msg] = await _outbox(sm)
    assert (msg.category, msg.level, msg.source) == ("system", "info", "system")
    lines = msg.text.split("\n")
    assert lines[0] == "✅ Pronti per l'apertura — 15/03/2027", msg.text
    assert "Sistema: servizi attivi; database e Redis ok" in lines
    assert "Mercati aperti oggi: NYSE, NASDAQ, MTA, XETRA" in lines
    assert "Quotazioni: 2 simboli aggiornati" in lines
    assert 3 <= len(lines) - 1 <= 6
    assert await _activity_count(sm) == before  # never in the activity feed
    async with sm() as s:
        assert await job_done(s, JOB, "2027-03-15")


async def test_problems_are_listed(env):
    sm, _, _ = env
    await _configure(sm)
    await _portfolio(sm, "Test 260 A", symbols=["AAPL", "MSFT"])
    await _portfolio(sm, "Test 260 Bloccato", status=PortfolioStatus.locked)
    await _portfolio(sm, "Test 260 Riduzione", status=PortfolioStatus.reduce_only)
    await _portfolio(sm, "Test 260 Archiviato", status=PortfolioStatus.archived)
    async with sm() as s:
        await set_kill_switch(s, True)
        await halts.set_halt(s, scope="global", portfolio_id=None, reason="verifica dati",
                             by="admin:admin@example.com")
        discard_post_commit(s)
        await s.commit()
    clock = Clock(rome(MON, 8, 45))
    qs = _quotes(clock.t, "AAPL")
    qs.set_quote("AAPL", D("10"), D("10.1"), D("10"), ts=clock.t - timedelta(days=6))
    before = await _activity_count(sm)
    await _check(env, qs, clock).tick()  # no heartbeat, no BrokerSync / monitor lock
    [msg] = await _outbox(sm)
    assert msg.category == "system" and msg.level == "warning"
    lines = msg.text.split("\n")
    problems = [line for line in lines if line.startswith("• ")]
    assert lines[0] == f"⚠️ Controllo pre-apertura: {len(problems)} problemi — 15/03/2027"
    assert set(problems) == {
        "• Quotazioni non disponibili: MSFT",
        "• Quotazioni vecchie: AAPL",
        "• Battito del worker assente",
        "• Sincronizzazione broker non attiva",
        "• Monitor del denaro reale non attivo",
        "• Kill switch attivo",
        "• Blocco aperture globale attivo: verifica dati",
        "• Portafogli bloccati: Test 260 Bloccato",
        "• Portafogli in sola riduzione: Test 260 Riduzione",
    }, msg.text
    assert "Archiviato" not in msg.text
    assert await _activity_count(sm) == before


async def test_stale_heartbeat_and_symbol_cap(env):
    sm, engine, redis = env
    await _configure(sm)
    symbols = [f"S{i:02d}" for i in range(25)]
    await _portfolio(sm, "Test 260 A", symbols=symbols)
    clock = Clock(rome(MON, 8, 45))
    qs = _quotes(clock.t, *symbols[:20])
    await redis.set(HEARTBEAT_KEY, "1")  # 1970
    async with Locks(engine):
        await _check(env, qs, clock).tick()
    [msg] = await _outbox(sm)
    problems = [line for line in msg.text.split("\n") if line.startswith("• ")]
    # S20-S24 have no quote: only the first 20 symbols are checked
    assert problems == [problems[0]] and problems[0].startswith("• Worker senza battito da ")


async def test_weekend_skipped(env):
    sm, _, _ = env
    await _configure(sm)
    clock = Clock(rome(13, 9))  # Saturday
    await _check(env, NoQuotes(), clock).tick()
    clock.t = rome(14, 9)  # Sunday
    await _check(env, NoQuotes(), clock).tick()
    assert await _outbox(sm) == []
    async with sm() as s:
        assert not await job_done(s, JOB, "2027-03-13")


async def test_all_markets_closed_one_line(env):
    sm, _, _ = env
    await _configure(sm)
    await _portfolio(sm, "Test 260 A", symbols=["AAPL"])
    clock = Clock(rome(25, 8, 45, month=12, year=2026))  # Christmas, a Friday
    await _check(env, NoQuotes(), clock).tick()
    [msg] = await _outbox(sm)
    assert msg.text == "📅 Mercati chiusi oggi — 25/12/2026"
    assert (msg.category, msg.level) == ("system", "info")


async def test_setting_off_and_custom_time(env):
    sm, engine, redis = env
    await _configure(sm)
    await _setting(sm, "preopen_check", {"on": False, "at": "08:45"})
    clock = Clock(rome(MON, 8, 50))
    await _check(env, NoQuotes(), clock).tick()
    assert await _outbox(sm) == []

    await _setting(sm, "preopen_check", {"on": True, "at": "09:30"})
    qs = FakeQuoteSource()
    async with Locks(engine):
        await write_heartbeat(redis)
        await _check(env, qs, clock).tick()
        assert await _outbox(sm) == []
        clock.t = rome(MON, 9, 30)
        await _check(env, qs, clock).tick()
    [msg] = await _outbox(sm)
    assert msg.text.startswith("✅ Pronti per l'apertura")
    assert "Quotazioni: nessun simbolo da controllare" in msg.text


async def test_not_ready_waits_then_window_ends(env, caplog):
    sm, engine, redis = env
    clock = Clock(rome(MON, 8, 45))
    await _check(env, NoQuotes(), clock).tick()  # Telegram not configured
    async with sm() as s:
        assert not await job_done(s, JOB, "2027-03-15")
    await _configure(sm)
    clock.t = rome(MON, 9, 10)
    async with Locks(engine):
        await write_heartbeat(redis)
        await _check(env, FakeQuoteSource(), clock).tick()
    assert len(await _outbox(sm)) == 1

    clock.t = rome(16, 10, 45)  # Tuesday, after the 2-hour window: skipped, claimed
    with caplog.at_level(logging.WARNING, logger="smtrading.telegram.preopen"):
        await _check(env, NoQuotes(), clock).tick()
    assert len(await _outbox(sm)) == 1
    assert "skipped" in caplog.text
    async with sm() as s:
        assert await job_done(s, JOB, "2027-03-16")


async def test_many_halts_and_portfolios_fit_in_telegram(env):
    sm, engine, redis = env
    await _configure(sm)
    pids = [await _portfolio(sm, f"Test 260 Portafoglio {i:02d}") for i in range(30)]
    for i in range(8):
        await _portfolio(sm, f"Test 260 Bloccato {i}", status=PortfolioStatus.locked)
    async with sm() as s:
        for pid in pids:
            await halts.set_halt(s, scope="portfolio", portfolio_id=pid, reason="x" * 400,
                                 by="admin:admin@example.com")
        discard_post_commit(s)
        await s.commit()
    clock = Clock(rome(MON, 8, 45))
    async with Locks(engine):
        await write_heartbeat(redis)
        await _check(env, FakeQuoteSource(), clock).tick()
    out = await _outbox(sm)
    assert out and all(utf16_len(m.text) <= MAX_LEN for m in out)
    text_ = "\n".join(m.text for m in out)
    halt_lines = [ln for ln in text_.split("\n") if ln.startswith("• Blocco aperture su ")]
    assert len(halt_lines) == 5
    assert "• … e altri 25 blocchi aperture" in text_
    assert ("• Portafogli bloccati: Test 260 Bloccato 0, Test 260 Bloccato 1, "
            "Test 260 Bloccato 2, Test 260 Bloccato 3, Test 260 Bloccato 4 … e altri 3") in text_


async def test_broker_link_health(env):
    sm, engine, redis = env
    await _configure(sm)
    clock = Clock(rome(MON, 8, 45))
    async with sm() as s:
        s.add_all([
            BrokerLink(name="Alpaca prova", kind="alpaca_paper", enabled=True, status="error",
                       last_error="401  unauthorized:\n" + "y" * 300, health_failures=3),
            BrokerLink(name="Spento", kind="t212_demo", enabled=False, status="error",
                       last_error="vecchio"),
            BrokerLink(name="T212 reale", kind="t212_live", enabled=True, status="ok",
                       live_state="armed", last_snapshot_at=clock.t - timedelta(minutes=45)),
            BrokerLink(name="Alpaca reale", kind="alpaca_live", enabled=True, status="ok",
                       live_state="dry_run", last_snapshot_at=clock.t - timedelta(minutes=5)),
            BrokerLink(name="Mai letto", kind="alpaca_live", enabled=True, status="ok",
                       live_state="armed"),
            BrokerLink(name="Solo configurato", kind="alpaca_live", enabled=True, status="ok",
                       live_state="configured"),
        ])
        await s.commit()
    async with Locks(engine):
        await write_heartbeat(redis)
        await _check(env, FakeQuoteSource(), clock).tick()
    [msg] = await _outbox(sm)
    problems = sorted(ln for ln in msg.text.split("\n") if ln.startswith("• "))
    assert len(problems) == 3, msg.text
    assert problems[0].startswith("• Collegamento broker Alpaca prova in errore: 401 unauthorized: y")
    assert problems[0].endswith("…") and len(problems[0]) < 140
    assert problems[1] == "• Collegamento broker Mai letto: nessun dato del conto"
    assert problems[2] == "• Collegamento broker T212 reale: dati del conto fermi da 45 min"


async def _paperclip(sm, *, enabled: bool, **state) -> None:
    from smtrading.paperclip.config import DEFAULT_STATE, SETTINGS_KEY, STATE_KEY

    await _setting(sm, SETTINGS_KEY, {"base_url": "http://paperclip.local", "company_id": "c1",
                                      "enabled": enabled, "agent_map": {}})
    await _setting(sm, STATE_KEY, {**DEFAULT_STATE, **state})


async def _preopen_problems(env, clock) -> list[str]:
    """One run of the check (the day forgotten first); the caller holds the locks."""
    sm, _, redis = env
    async with sm() as s:
        await s.execute(text("delete from job_runs"))
        await s.execute(text("delete from telegram_outbox"))
        await s.commit()
    await write_heartbeat(redis)
    await _check(env, FakeQuoteSource(), clock).tick()
    [msg] = await _outbox(sm)
    return [ln for ln in msg.text.split("\n") if ln.startswith("• ")]


async def test_paperclip_reader_line(env):
    sm, engine, _ = env
    await _configure(sm)
    async with Locks(engine):
        await _paperclip_cases(env, Clock(rome(MON, 8, 45)))


async def _paperclip_cases(env, clock) -> None:
    sm, _, _ = env
    # disabled: nothing, whatever the state says
    await _paperclip(sm, enabled=False, consecutive_errors=9, isolation="off",
                     last_error="boom")
    assert await _preopen_problems(env, clock) == []
    # enabled and healthy: nothing
    await _paperclip(sm, enabled=True, isolation="ok")
    assert await _preopen_problems(env, clock) == []
    # enabled, 2 failed reads: not yet unreachable (isolation still ok)
    await _paperclip(sm, enabled=True, isolation="ok", consecutive_errors=2,
                     last_error="timeout")
    assert await _preopen_problems(env, clock) == []
    # enabled, 3 failed reads: unreachable, with the short reason
    await _paperclip(sm, enabled=True, isolation="ok", consecutive_errors=3,
                     last_error="HTTP 503  da Paperclip\n" + "x" * 300)
    [line] = await _preopen_problems(env, clock)
    assert line.startswith("• Paperclip non raggiungibile da 3 letture: HTTP 503 da Paperclip x")
    assert line.endswith("…") and len(line) < 140
    # isolation changed
    await _paperclip(sm, enabled=True, isolation="changed",
                     changes={"removed": ["p1"], "changed": ["p2", "p3"], "added": []})
    assert await _preopen_problems(env, clock) == [
        "• Isolamento Paperclip cambiato: 1 regole rimosse, 2 modificate"]
    # isolation never verified
    await _paperclip(sm, enabled=True, isolation="unknown", last_error=None)
    assert await _preopen_problems(env, clock) == [
        "• Isolamento Paperclip non verificato: nessuna lettura riuscita"]
