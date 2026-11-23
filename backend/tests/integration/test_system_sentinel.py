"""«Sentinella di sistema» (worker job): every check against a real database and Redis,
Paperclip through a fake client (GET only), «Salute del sistema» through a fake view and
a controlled clock; one Telegram message per pass (category system, never the agents'
activity feed), deduplicated across restarts."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity, Decision
from smtrading.agents.models import Agent, AgentPortfolio, McpAuditLog
from smtrading.common.db import make_engine
from smtrading.paperclip import cycles
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import save_config
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import AppSetting
from smtrading.sentinel import errorlog
from smtrading.sentinel.job import SystemSentinel
from smtrading.sentinel.settings import SETTINGS_KEY, STATE_KEY
from smtrading.telegram.models import TelegramOutbox


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakePaperclip:
    def __init__(self):
        self.agents_list: list[dict] = [{"id": "pc-rm", "name": "Risk Manager",
                                         "status": "idle"}]
        self.runs: list[dict] = []
        self.issue_list: list[dict] = []
        self.fail: str | None = None
        self.calls: list[str] = []

    def __call__(self, cfg):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def agents(self):
        self.calls.append("agents")
        if self.fail:
            raise PaperclipError(self.fail)
        return self.agents_list

    async def heartbeat_runs(self, limit):
        self.calls.append(f"runs:{limit}")
        return self.runs

    async def issues(self, limit):
        self.calls.append(f"issues:{limit}")
        if self.fail:
            raise PaperclipError(self.fail)
        return self.issue_list


class FakeHealth:
    def __init__(self):
        self.view = {"status": "ok", "reasons": []}
        self.fail = False

    async def __call__(self, session, redis, *, now):
        if self.fail:
            raise RuntimeError("health broken")
        return self.view


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=True, agent_map={}, by="t")
        await s.commit()
    yield sm, redis
    await redis.aclose()
    await engine.dispose()


def _now():
    return datetime.now(UTC).replace(microsecond=0)


def _sentinel(env, clock, fake=None, health=None):
    sm, redis = env
    return SystemSentinel(sm, redis, client_factory=fake or FakePaperclip(), clock=clock,
                          tz="Europe/Rome", health=health or FakeHealth())


async def _outbox(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _texts(sm) -> list[str]:
    return [o.text for o in await _outbox(sm)]


async def _activity_count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(Activity))


async def _agent(sm, name) -> uuid.UUID:
    async with sm() as s:
        a = Agent(name=name, token_prefix=uuid.uuid4().hex[:8], token_hash="x")
        s.add(a)
        await s.commit()
        return a.id


async def _portfolio(sm, name, agent_id=None) -> uuid.UUID:
    async with sm() as s:
        p = Portfolio(name=name)
        s.add(p)
        await s.flush()
        if agent_id is not None:
            s.add(AgentPortfolio(agent_id=agent_id, portfolio_id=p.id, permission="trade"))
        await s.commit()
        return p.id


# -- 1. health, dedup, reminder, resolve, restart --------------------------------------------

async def test_health_problem_told_once_reminded_resolved_and_survives_restart(env):
    sm, _ = env
    clock, health = Clock(_now()), FakeHealth()
    activity = await _activity_count(sm)
    await _sentinel(env, clock, health=health).tick()
    assert await _texts(sm) == []
    health.view = {"status": "problema", "reasons": ["worker fermo: nessun battito"]}
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, health=health).tick()
    rows = await _outbox(sm)
    assert len(rows) == 1
    assert (rows[0].source, rows[0].category, rows[0].level) == ("system", "system",
                                                                 "critical")
    assert "Nuovo problema: Salute del sistema in «problema»" in rows[0].text
    assert "worker fermo" in rows[0].text
    for _ in range(3):  # a new instance each time: a restart repeats nothing
        clock.t += timedelta(minutes=5)
        await _sentinel(env, clock, health=health).tick()
    assert len(await _texts(sm)) == 1
    clock.t += timedelta(hours=2)
    await _sentinel(env, clock, health=health).tick()
    texts = await _texts(sm)
    assert len(texts) == 2 and "Ancora aperto da" in texts[1]
    health.view = {"status": "ok", "reasons": []}
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, health=health).tick()
    texts = await _texts(sm)
    assert len(texts) == 3 and "🟢 Risolto: Salute del sistema in «problema»" in texts[2]
    assert (await _outbox(sm))[2].level == "info"
    assert await _activity_count(sm) == activity  # never the agents' feed


async def test_attention_is_told_only_after_30_minutes(env):
    sm, _ = env
    clock, health = Clock(_now()), FakeHealth()
    health.view = {"status": "attenzione", "reasons": ["quotazioni vecchie"]}
    await _sentinel(env, clock, health=health).tick()
    clock.t += timedelta(minutes=25)
    await _sentinel(env, clock, health=health).tick()
    assert await _texts(sm) == []
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, health=health).tick()
    (text,) = await _texts(sm)
    assert "attenzione" in text and "quotazioni vecchie" in text


async def test_off_does_nothing(env):
    sm, _ = env
    async with sm() as s:
        s.add(AppSetting(key=SETTINGS_KEY, value={"on": False}))
        await s.commit()
    health = FakeHealth()
    health.view = {"status": "problema", "reasons": ["x"]}
    fake = FakePaperclip()
    await _sentinel(env, Clock(_now()), fake, health).tick()
    assert await _texts(sm) == [] and fake.calls == []


async def test_a_failing_check_does_not_stop_the_others_and_is_recorded(env):
    sm, _ = env
    health = FakeHealth()
    health.fail = True
    fake = FakePaperclip()
    fake.agents_list[0]["status"] = "error"
    clock = Clock(_now())
    s = _sentinel(env, clock, fake, health)
    await s.tick()
    clock.t += timedelta(minutes=10)
    await s.tick()
    (text,) = await _texts(sm)
    assert "Agente Risk Manager in errore" in text
    async with sm() as session:
        state = await session.scalar(select(AppSetting.value).where(AppSetting.key == STATE_KEY))
    assert "salute" in state["last_error"]


# -- 2. Paperclip agents, runs, issues -----------------------------------------------------------

async def test_agent_in_error_after_10_minutes_kept_while_paperclip_is_unreachable(env):
    sm, _ = env
    fake, clock = FakePaperclip(), Clock(_now())
    fake.agents_list[0]["status"] = "error"
    fake.runs = [{"id": "r1", "agentId": "pc-rm", "status": "failed",
                  "errorMessage": "adapter crashed",
                  "finishedAt": (clock.t - timedelta(minutes=2)).isoformat()}]
    await _sentinel(env, clock, fake).tick()
    (first,) = await _texts(sm)  # the failed run at once (an event)
    assert "Ciclo dell'agente Risk Manager non riuscito (fallito)" in first
    clock.t += timedelta(minutes=10)
    await _sentinel(env, clock, fake).tick()
    texts = await _texts(sm)
    assert len(texts) == 2 and "Agente Risk Manager in errore" in texts[1]
    assert "adapter crashed" in texts[1]
    assert set(fake.calls) == {"agents", "runs:100", "issues:200"}
    fake.fail = "timeout"
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, fake).tick()
    assert len(await _texts(sm)) == 2  # not resolved by a failed read
    fake.fail = None
    fake.agents_list[0]["status"] = "idle"
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, fake).tick()
    texts = await _texts(sm)
    assert len(texts) == 3 and "Risolto: Agente Risk Manager in errore" in texts[2]


async def test_paperclip_disabled_reads_nothing(env):
    sm, _ = env
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=False, agent_map={}, by="t")
        await s.commit()
    fake = FakePaperclip()
    await _sentinel(env, Clock(_now()), fake).tick()
    assert fake.calls == []


# -- 3. missed cycles ------------------------------------------------------------------------------

async def test_a_missed_routine_fire_is_told_once(env):
    sm, _ = env
    now = _now()
    fire = (now - timedelta(minutes=40)).replace(second=0)
    async with sm() as s:
        await cycles.save_cycles(s, {
            "routines": [{"id": "rt1", "title": "Scansione", "status": "active",
                          "agent_id": "pc-rm",
                          "triggers": [{"cron": f"{fire.minute} {fire.hour} * * *",
                                        "tz": "UTC", "enabled": True}]}],
            "executions": [], "read_at": now.isoformat(), "error": None})
        await s.commit()
    clock = Clock(now)
    await _sentinel(env, clock).tick()
    (text,) = await _texts(sm)
    assert "Ciclo mancato: «Scansione» (Risk Manager)" in text
    clock.t += timedelta(minutes=5)
    async with sm() as s:  # the reader keeps the routines fresh
        state = await cycles.load_cycles(s)
        state["read_at"] = clock.t.isoformat()
        await cycles.save_cycles(s, state)
        await s.commit()
    await _sentinel(env, clock).tick()
    assert len(await _texts(sm)) == 1


# -- 4. MCP errors --------------------------------------------------------------------------------

async def test_repeated_mcp_errors_and_internal_errors(env):
    sm, _ = env
    now = _now()
    agent = await _agent(sm, "Executor")
    async with sm() as s:
        for i in range(5):
            s.add(McpAuditLog(agent_id=agent, method="place_order", params={},
                              outcome="FORBIDDEN", latency_ms=1,
                              created_at=now - timedelta(minutes=i)))
        s.add(McpAuditLog(agent_id=agent, method="get_positions", params={},
                          outcome="INTERNAL", latency_ms=1, created_at=now))
        s.add(McpAuditLog(agent_id=agent, method="admin:x", params={}, outcome="INTERNAL",
                          latency_ms=1, created_at=now))
        await s.commit()
    await _sentinel(env, Clock(now)).tick()
    (text,) = await _texts(sm)
    assert "Executor: 5 errori MCP su place_order in 30 min" in text
    assert "1 errore interno MCP negli ultimi 30 min" in text


# -- 5. error logs ---------------------------------------------------------------------------------

async def test_error_log_rate_from_redis(env):
    sm, redis = env
    now = _now()
    counter = errorlog.ErrorCounter("api")
    for _ in range(6):
        record = logging.LogRecord("smtrading.mcp.server", logging.ERROR, __file__, 1,
                                   "tool crashed password=hunter2hunter2", None, None)
        record.created = now.timestamp()
        counter.handle(record)
    await errorlog.flush(redis, counter)
    await _sentinel(env, Clock(now)).tick()
    (text,) = await _texts(sm)
    assert "6 errori nei log dell'api (smtrading.mcp.server) in 15 min" in text
    assert "hunter2" not in text


# -- 6. identities and authors ---------------------------------------------------------------------

async def test_identity_burst_and_decision_author_mismatch(env):
    sm, _ = env
    now = _now()
    minute = now.replace(second=10) - timedelta(minutes=3)
    agents = [await _agent(sm, n) for n in ("Risk Manager", "Executor", "Analyst", "CEO")]
    async with sm() as s:
        for a in agents:
            s.add(McpAuditLog(agent_id=a, method="get_quote", params={}, outcome="ok",
                              latency_ms=1, created_at=minute))
        s.add(Decision(agent_id=agents[1], kind="other", summary="x",
                       details={"by": "Risk Manager"}, symbols=[]))
        s.add(Decision(agent_id=agents[0], kind="other", summary="y",
                       details={"by": "risk manager"}, symbols=[]))
        await s.commit()
    await _sentinel(env, Clock(now)).tick()
    rows = await _outbox(sm)
    assert len(rows) == 1 and rows[0].level == "critical"
    assert "4 identità di agenti diverse in un solo minuto su MCP" in rows[0].text
    assert "firmata da Executor ma scritta «da» Risk Manager" in rows[0].text
    assert "firmata da Risk Manager" not in rows[0].text


# -- 7. leaks -------------------------------------------------------------------------------------

async def test_activity_rows_no_agent_can_see_are_leaks_once(env):
    sm, _ = env
    now = _now()
    agent = await _agent(sm, "Executor")
    owner_only = await _portfolio(sm, "Prova del proprietario")
    shared = await _portfolio(sm, "Aggressivo", agent)
    async with sm() as s:
        for pid, type_ in ((owner_only, "broker.reconciliation_diff"), (shared, "order.filled"),
                           (None, "portfolio.cash"), (None, "instrument.added"),
                           (None, "live.armed")):
            s.add(Activity(type=type_, level="info", portfolio_id=pid, agent_id=None,
                           summary="s", data={}))
        s.add(Activity(type="price_alert", level="info", portfolio_id=None, agent_id=agent,
                       summary="s", data={}))
        await s.commit()
    clock = Clock(now + timedelta(seconds=5))
    await _sentinel(env, clock).tick()
    (text,) = await _texts(sm)
    assert "«Prova del proprietario», un portafoglio a cui nessun agente ha accesso" in text
    assert "broker.reconciliation_diff" in text
    assert "1 riga del registro visibile a tutti gli agenti" in text
    assert "portfolio.cash" in text
    assert "Aggressivo" not in text and "instrument.added" not in text
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock).tick()
    assert len(await _texts(sm)) == 1  # the cursor moved on


# -- 8. weekly summary -------------------------------------------------------------------------------

async def test_weekly_summary_on_sunday_evening_once(env):
    sm, _ = env
    health = FakeHealth()
    health.view = {"status": "problema", "reasons": ["x"]}
    sunday = datetime(2026, 11, 29, 18, 0, tzinfo=UTC)  # 19:00 in Rome
    clock = Clock(sunday)
    await _sentinel(env, clock, health=health).tick()
    clock.t = sunday + timedelta(hours=1)  # 20:00 in Rome
    await _sentinel(env, clock, health=health).tick()
    clock.t += timedelta(minutes=5)
    await _sentinel(env, clock, health=health).tick()
    texts = await _texts(sm)
    weekly = [t for t in texts if "Riepilogo della settimana" in t]
    assert len(weekly) == 1
    assert "Salute del sistema: 1" in weekly[0] and "Ancora aperti" in weekly[0]


async def test_the_real_system_health_view_is_read(env):
    """No fake: a fresh system (no worker heartbeat, no provider) is «problema»."""
    sm, redis = env
    await SystemSentinel(sm, redis, client_factory=FakePaperclip(), clock=Clock(_now()),
                         tz="Europe/Rome").tick()
    (text,) = await _texts(sm)
    assert "Salute del sistema in «problema»" in text and "worker fermo" in text
