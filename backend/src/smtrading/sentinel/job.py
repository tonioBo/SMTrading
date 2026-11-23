"""«Sentinella di sistema» (worker job, every 5 minutes, behind the startup gate): the
checks an external watcher used to run, inside SMTrading. Each pass:

1. «Salute del sistema» (`admin.system_health`): «problema» at once, «attenzione» when it
   lasts HEALTH_WARN_AFTER_S;
2. Paperclip (GET only, `PaperclipClient`): agents in error / paused / unknown for
   AGENT_BAD_AFTER_S, failed heartbeat runs, blocked issues;
3. missed cycles: the routines' fires with no execution (`paperclip.cycles`, read by the
   reader) after CYCLE_GRACE;
4. repeated MCP errors (one agent, one tool) and any internal MCP error;
5. the ERROR/CRITICAL records per logger of the api and the worker (`sentinel.errorlog`);
6. identity bursts on MCP and decisions signed by one agent but written «by» another;
7. activity rows the agents could see but should not: rows of a portfolio no agent can
   access, global rows with no agent that are not meant for every agent.

What to tell is decided by `sentinel.engine` (one message at the start, a reminder every
2 h, one «Risolto»); the state lives in `app_settings["system_sentinel_state"]`, so a
restart repeats nothing. One Telegram message per pass (category `system`, never the
agents' activity feed) and, on Sunday from 20:00, the week's summary. A check that fails
is logged and skipped (its open problems are kept); the job never raises out of `run`.
Off with `app_settings["system_sentinel"]`. No transaction is open during a Paperclip
request."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity, Decision
from smtrading.agents.models import Agent, AgentPortfolio, McpAuditLog
from smtrading.common.config import get_settings
from smtrading.paperclip import cycles as paperclip_cycles
from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import AppSetting
from smtrading.sentinel import checks, engine, errorlog
from smtrading.sentinel.engine import Finding
from smtrading.sentinel.settings import STATE_KEY, sentinel_on
from smtrading.telegram.outbox import enqueue
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

INTERVAL_S = 300
RUNS_LIMIT = 100
ISSUES_LIMIT = 200
LEAK_MAX_ROWS = 500

ClientFactory = Callable[[PaperclipConfig], PaperclipClient]
HealthFn = Callable[..., Awaitable[dict[str, Any]]]

PAPERCLIP_KINDS = ("agent", "run", "issue", "cycle")


async def _default_health(session: AsyncSession, redis, *, now: datetime) -> dict[str, Any]:
    from smtrading.admin.system_health import system_health  # the api side's view

    return await system_health(session, redis, now=now)


class SystemSentinel:
    def __init__(self, sessionmaker: async_sessionmaker, redis, *,
                 client_factory: ClientFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 interval_s: float = INTERVAL_S, health: HealthFn | None = None) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._client_factory = client_factory or (lambda cfg: PaperclipClient(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tz = tz or get_settings().tz
        self._interval_s = interval_s
        self._health = health or _default_health
        # this pass's Paperclip agents and runs (None: not read), for the identity check
        self._paperclip: tuple[list[dict], list[dict]] | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await job_tick()  # «Salute del sistema»: last pass
            except Exception:
                log.warning("system sentinel failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        now = self._clock()
        async with self._sm() as session:
            if not await sentinel_on(session):
                return
            cursor = (await self._state(session))["cursors"].get("activity")
        findings: list[Finding] = []
        covered: set[str] = set()
        errors: list[str] = []
        cursors: dict[str, Any] = {}

        async def guarded(name: str, kinds: tuple[str, ...], fn) -> None:
            try:
                result = await fn()
            except Exception as e:  # one check never stops the others
                log.warning("system sentinel: %s check failed", name, exc_info=True)
                errors.append(f"{name}: {getattr(e, 'message', None) or type(e).__name__}")
                return
            if result is None:
                return
            findings.extend(result)
            covered.update(kinds)

        self._paperclip = None
        await guarded("salute", ("health",), lambda: self._check_health(now))
        await guarded("paperclip", (), lambda: self._check_paperclip(now, covered, errors))
        await guarded("mcp", ("mcp",), lambda: self._check_mcp(now))
        await guarded("log", ("errlog",), lambda: self._check_errlog(now))
        await guarded("identità", ("identity", "author"), lambda: self._check_security(now))
        await guarded("registro", ("leak",), lambda: self._check_leaks(now, cursor, cursors))
        await self._record(now, findings, covered, errors, cursors)

    # -- checks ----------------------------------------------------------------------------

    async def _check_health(self, now: datetime) -> list[Finding]:
        async with self._sm() as session:
            view = await self._health(session, self._redis, now=now)
        return checks.health_findings(view)

    async def _check_paperclip(self, now: datetime, covered: set[str],
                               errors: list[str]) -> list[Finding]:
        """Adds to `covered` itself: agents, runs, issues and cycles succeed apart."""
        async with self._sm() as session:
            cfg = await load_config(session)
            cycles_state = await paperclip_cycles.load_cycles(session)
        if not (cfg.enabled and cfg.complete and not cfg.unreadable):
            covered.update(PAPERCLIP_KINDS)  # nothing to watch: open problems resolve
            return []
        out: list[Finding] = []
        agents: list[dict] = []
        async with self._client_factory(cfg) as client:
            try:
                agents = await client.agents()
                runs = await client.heartbeat_runs(RUNS_LIMIT)
                self._paperclip = (agents, runs)
                out += checks.agent_findings(agents, runs, now)
                covered.update(("agent", "run"))
            except PaperclipError as e:
                errors.append(f"paperclip agenti: {e.message}")
            try:
                out += checks.issue_findings(await client.issues(ISSUES_LIMIT))
                covered.add("issue")
            except PaperclipError as e:
                errors.append(f"paperclip task: {e.message}")
        names = {str(a.get("id")): str(a.get("name")) for a in agents
                 if a.get("id") and a.get("name")}
        missed = checks.cycle_findings(cycles_state, now, self._tz, names)
        if missed is not None:
            out += missed
            covered.add("cycle")
        return out

    async def _check_mcp(self, now: datetime) -> list[Finding]:
        async with self._sm() as session:
            rows = (await session.execute(
                select(McpAuditLog.agent_id, Agent.name, McpAuditLog.method,
                       McpAuditLog.outcome)
                .outerjoin(Agent, Agent.id == McpAuditLog.agent_id)
                .where(McpAuditLog.created_at >= now - checks.MCP_WINDOW,
                       McpAuditLog.outcome.notin_(("ok", "OK")),
                       ~McpAuditLog.method.like("admin:%")))).all()
        return checks.mcp_findings([tuple(r) for r in rows])

    async def _check_errlog(self, now: datetime) -> list[Finding]:
        return checks.errlog_findings(await errorlog.read_counts(self._redis, now.timestamp()))

    async def _check_security(self, now: datetime) -> list[Finding]:
        since = now - checks.SECURITY_WINDOW
        minute = func.date_trunc("minute", McpAuditLog.created_at)
        async with self._sm() as session:
            calls = (await session.execute(
                select(minute, Agent.name).distinct()
                .join(Agent, Agent.id == McpAuditLog.agent_id)
                .where(McpAuditLog.created_at >= since,
                       ~McpAuditLog.method.like("admin:%")))).all()
            bursts = (await session.execute(
                select(minute, func.count(func.distinct(McpAuditLog.agent_id)))
                .where(McpAuditLog.created_at >= since, McpAuditLog.agent_id.is_not(None),
                       ~McpAuditLog.method.like("admin:%"))
                .group_by(minute)
                .having(func.count(func.distinct(McpAuditLog.agent_id))
                        >= checks.IDENTITY_BURST))).all()
            decisions = (await session.execute(
                select(Decision.id, Agent.name, Decision.details["by"].astext)
                .join(Agent, Agent.id == Decision.agent_id)
                .where(Decision.created_at >= since, Decision.details.has_key("by"))
                .order_by(Decision.id))).all()
        if self._paperclip is not None:
            identity = checks.unexplained_calls(
                [(m, n) for m, n in calls], self._tz, checks.run_spans(*self._paperclip))
        else:
            identity = checks.identity_findings([(m, int(n)) for m, n in bursts], self._tz)
        return (identity
                + checks.author_findings([(int(i), n, b or "") for i, n, b in decisions]))

    async def _check_leaks(self, now: datetime, cursor: Any,
                           cursors: dict[str, Any]) -> list[Finding]:
        no_agent = ~exists().where(AgentPortfolio.portfolio_id == Activity.portfolio_id)
        leak = or_(
            and_(Activity.portfolio_id.is_not(None), no_agent),
            and_(Activity.portfolio_id.is_(None), Activity.agent_id.is_(None),
                 ~Activity.type.like("live.%"), Activity.type.notin_(checks.GLOBAL_TYPES)))
        conditions = [Activity.ts >= now - checks.LEAK_WINDOW]
        if isinstance(cursor, int):
            conditions.append(Activity.id > cursor)
        async with self._sm() as session:
            top = await session.scalar(select(func.max(Activity.id)).where(*conditions))
            if top is None:
                cursors["activity"] = cursor
                return []
            rows = (await session.execute(
                select(Activity.id, Activity.portfolio_id, Portfolio.name, Activity.type)
                .outerjoin(Portfolio, Portfolio.id == Activity.portfolio_id)
                .where(*conditions, Activity.id <= top, leak)
                .order_by(Activity.id).limit(LEAK_MAX_ROWS))).all()
        cursors["activity"] = int(top)
        return checks.leak_findings([tuple(r) for r in rows])

    # -- state and messages ----------------------------------------------------------------

    @staticmethod
    async def _state(session: AsyncSession, *, for_update: bool = False) -> dict[str, Any]:
        query = select(AppSetting.value).where(AppSetting.key == STATE_KEY)
        return engine.load(await session.scalar(
            query.with_for_update() if for_update else query))

    async def _record(self, now: datetime, findings: list[Finding], covered: set[str],
                      errors: list[str], cursors: dict[str, Any]) -> None:
        async with self._sm() as session, session.begin():
            await session.execute(insert(AppSetting).values(
                key=STATE_KEY, value=engine.fresh_state())
                .on_conflict_do_nothing(index_elements=["key"]))
            state = await self._state(session, for_update=True)
            notes = engine.step(state, findings, now, covered)
            if notes:
                level, text = engine.compose(notes, now, self._tz)
                await enqueue(session, source="system", category="system", level=level,
                              text=text)
                log.warning("system sentinel: %s", "; ".join(
                    f"{n.what} {n.kind}" for n in notes))
            if engine.weekly_due(state, now, self._tz):
                await enqueue(session, source="system", category="system", level="info",
                              text=engine.weekly_summary(state, now, self._tz))
                engine.mark_weekly(state, now, self._tz)
            state["cursors"].update(cursors)
            state["last_pass_at"] = now.isoformat()
            state["last_error"] = "; ".join(errors)[:300] or None
            stmt = insert(AppSetting).values(key=STATE_KEY, value=state)
            await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                             set_={"value": state}))
