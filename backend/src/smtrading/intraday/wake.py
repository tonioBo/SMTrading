"""Mode A wake: a signal trigger becomes a Paperclip issue for the agent configured on the
portfolio (`intraday_wake_agent_id`), or a Telegram message when no agent can be woken.
Never the activity feed the agents read, and nothing for a portfolio no agent can access.

Every trigger gets one `intraday_triggers` row. The outcome is decided in one transaction
(late, no agent access, no usable wake agent, hourly cap), then Paperclip is called with no
transaction open (a `pending` row stands for the call in flight), then the outcomes are
written in a second transaction. A `pending` row older than STALE_PENDING (a crash between
the call and the write) is closed by looking for the issue by title: never a second issue.
One open issue per (portfolio, symbol): a further trigger on the same pair while the last
issue is still open is `issue_open`. Texts are built from the trigger and portfolio data
only."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.access import portfolios_with_agents
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.common.config import get_settings
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
from smtrading.intraday.watch import Trigger
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import PaperclipConfig, load_config
from smtrading.paperclip.issues import PaperclipIssues, resolve_agent
from smtrading.portfolios.models import Portfolio
from smtrading.signals.service import _it
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

IssuesFactory = Callable[[PaperclipConfig], PaperclipIssues]
STALE_PENDING = timedelta(minutes=10)
CAP_WINDOW = timedelta(hours=1)
ERROR_MAX = 200
PAPERCLIP_OFF = "Paperclip disattivato o incompleto"
NO_AGENT_LINE = "nessun agente da svegliare: imposta il destinatario nel portafoglio"
CLOSING_LINE = "Decidi tu se e come operare: SMTrading non ha inviato ordini."


def wake_title(t: Trigger, portfolio: Portfolio) -> str:
    kind = "entrata" if t.rule == "entry" else "uscita"
    return f"SEGNALE · {portfolio.code or portfolio.name} · {t.symbol} · {kind}"


def _num(d) -> str:
    return "n/d" if d is None else _it(d)


def wake_text(t: Trigger, portfolio: Portfolio, zone: ZoneInfo) -> str:
    bar = t.bar_close.astimezone(zone).strftime("%H:%M")
    return "\n".join([
        f"Strategia: {t.name} ({t.ref})",
        f"Timeframe: {t.timeframe}",
        f"Regola: {t.rule_text}",
        f"Valore: {_num(t.value)} · soglia: {_num(t.threshold)}",
        f"Prezzo: {_num(t.price)}",
        f"Ora della barra: {bar}",
        CLOSING_LINE,
    ])


def _from_row(r: IntradayTrigger) -> Trigger:
    return Trigger(
        portfolio_id=r.portfolio_id, ref=r.strategy_ref, name=r.strategy_name, symbol=r.symbol,
        rule="entry" if r.rule == "entry" else "exit", timeframe=r.timeframe,
        bar_close=r.bar_close, value=r.value, threshold=r.threshold, price=r.price,
        rule_text=r.rule_text, late=False)


@dataclass
class _Item:
    row_id: int
    trigger: Trigger
    title: str
    text: str
    agent_name: str | None = None


def _short(message: str) -> str:
    return message[:ERROR_MAX]


class Waker:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 issues_factory: IssuesFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None) -> None:
        self._sm = sessionmaker
        self._issues_factory = issues_factory or (lambda cfg: PaperclipIssues(cfg))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(tz or get_settings().tz)

    async def _usable_config(self) -> PaperclipConfig | None:
        async with self._sm() as session:
            cfg = await load_config(session)
        return cfg if (cfg.enabled and cfg.complete and not cfg.unreadable) else None

    async def handle(self, triggers: list[Trigger]) -> None:
        await self._close_stale()
        if triggers:
            items = await self._decide(triggers)
            if items:
                await self._wake(items)

    # -- stale pending rows: adopt the issue by title, never create a second one ----------

    async def _close_stale(self) -> None:
        cutoff = self._clock() - STALE_PENDING
        async with self._sm() as session:
            rows = list(await session.scalars(
                select(IntradayTrigger).where(IntradayTrigger.outcome == PENDING,
                                              IntradayTrigger.created_at < cutoff)
                .order_by(IntradayTrigger.id)))
            if not rows:
                return
            portfolios = {p.id: p for p in await session.scalars(
                select(Portfolio).where(Portfolio.id.in_({r.portfolio_id for r in rows})))}
            todo = [(r.id, wake_title(_from_row(r), portfolios[r.portfolio_id])) for r in rows]
        cfg = await self._usable_config()
        found: dict[int, str | None] = {}
        error = PAPERCLIP_OFF
        if cfg is not None:
            try:
                async with self._issues_factory(cfg) as client:
                    for row_id, title in todo:
                        found[row_id] = await client.open_issue_with_title(title)
                error = "compito non trovato"
            except PaperclipError as e:
                error = _short(e.message)
            except Exception:
                log.warning("intraday wake: stale pending lookup failed", exc_info=True)
                error = "errore inatteso"
        async with self._sm() as session, session.begin():
            for row_id, _ in todo:
                ident = found.get(row_id)
                await session.execute(
                    update(IntradayTrigger)
                    .where(IntradayTrigger.id == row_id, IntradayTrigger.outcome == PENDING)
                    .values(outcome=WOKEN if ident else FAILED, issue_ident=ident,
                            error=None if ident else error))

    # -- step 1: the outcome that needs no Paperclip, one transaction ---------------------

    async def _decide(self, triggers: list[Trigger]) -> list[_Item]:
        now = self._clock()
        pending: list[_Item] = []
        async with self._sm() as session, session.begin():
            ids = {t.portfolio_id for t in triggers}
            portfolios = {p.id: p for p in await session.scalars(
                select(Portfolio).where(Portfolio.id.in_(ids)))}
            reachable = await portfolios_with_agents(session, ids)
            agents = await self._wake_agents(session, portfolios.values())
            used = await self._recent_wakes(session, ids, now)
            for t in triggers:
                p = portfolios.get(t.portfolio_id)
                if p is None:
                    continue
                title, text = wake_title(t, p), wake_text(t, p, self._zone)
                agent_name = agents.get(p.id)
                telegram = None
                if t.late:
                    outcome = LATE
                elif p.id not in reachable:
                    outcome, telegram = TELEGRAM, f"{title}\n{text}"
                elif agent_name is None:
                    outcome, telegram = NO_AGENT, f"{title}\n{text}\n{NO_AGENT_LINE}"
                elif used[p.id] >= p.intraday_max_wakes_per_hour:
                    outcome = CAPPED
                else:
                    outcome = PENDING
                    used[p.id] += 1
                row = IntradayTrigger(
                    created_at=now, portfolio_id=p.id, strategy_ref=t.ref,
                    strategy_name=t.name, symbol=t.symbol, rule=t.rule, timeframe=t.timeframe,
                    bar_close=t.bar_close, value=t.value, threshold=t.threshold, price=t.price,
                    rule_text=t.rule_text, outcome=outcome)
                session.add(row)
                await session.flush()
                if telegram:
                    await enqueue(session, source="system", category="system",
                                  level="warning", text=telegram)
                if outcome == PENDING:
                    pending.append(_Item(row.id, t, title, text, agent_name))
        return pending

    @staticmethod
    async def _wake_agents(session: AsyncSession, portfolios) -> dict[uuid.UUID, str]:
        """Portfolio id -> name of its wake agent, only if active with `trade` on it."""
        out: dict[uuid.UUID, str] = {}
        wanted = {p.id: p.intraday_wake_agent_id for p in portfolios
                  if p.intraday_wake_agent_id is not None}
        if not wanted:
            return out
        rows = await session.execute(
            select(AgentPortfolio.portfolio_id, Agent.id, Agent.name)
            .join(Agent, Agent.id == AgentPortfolio.agent_id)
            .where(AgentPortfolio.portfolio_id.in_(set(wanted)),
                   AgentPortfolio.permission == "trade", Agent.active.is_(True)))
        for pid, agent_id, name in rows:
            if wanted.get(pid) == agent_id:
                out[pid] = name
        return out

    @staticmethod
    async def _recent_wakes(session: AsyncSession, ids: set[uuid.UUID],
                            now: datetime) -> dict[uuid.UUID, int]:
        used = {pid: 0 for pid in ids}
        rows = await session.execute(
            select(IntradayTrigger.portfolio_id, func.count())
            .where(IntradayTrigger.portfolio_id.in_(ids),
                   or_(IntradayTrigger.outcome == WOKEN, IntradayTrigger.outcome == PENDING),
                   IntradayTrigger.created_at >= now - CAP_WINDOW)
            .group_by(IntradayTrigger.portfolio_id))
        used.update({pid: n for pid, n in rows})
        return used

    # -- step 2: Paperclip, no transaction open -------------------------------------------

    async def _wake(self, items: list[_Item]) -> None:
        results: dict[int, tuple[str, str | None, str | None]] = {}
        cfg = await self._usable_config()
        if cfg is None:
            results = {i.row_id: (FAILED, None, PAPERCLIP_OFF) for i in items}
        else:
            last = await self._last_idents(items)
            try:
                async with self._issues_factory(cfg) as client:
                    open_ids = await client.open_identifiers()
                    assignees: dict[str, str] = {}
                    for it in items:
                        results[it.row_id] = await self._wake_one(
                            client, it, last, open_ids, assignees)
            except PaperclipError as e:
                for it in items:
                    results.setdefault(it.row_id, (FAILED, None, _short(e.message)))
            except Exception:
                log.warning("intraday wake: unexpected error", exc_info=True)
                for it in items:
                    results.setdefault(it.row_id, (FAILED, None, "errore inatteso"))
        await self._write(items, results)

    async def _last_idents(self, items: list[_Item]) -> dict[tuple[uuid.UUID, str], str]:
        out: dict[tuple[uuid.UUID, str], str] = {}
        async with self._sm() as session:
            for pid, symbol in {(i.trigger.portfolio_id, i.trigger.symbol) for i in items}:
                ident = await session.scalar(
                    select(IntradayTrigger.issue_ident)
                    .where(IntradayTrigger.portfolio_id == pid,
                           IntradayTrigger.symbol == symbol,
                           IntradayTrigger.issue_ident.is_not(None))
                    .order_by(IntradayTrigger.id.desc()).limit(1))
                if ident:
                    out[(pid, symbol)] = ident
        return out

    async def _wake_one(self, client: Any, it: _Item, last: dict, open_ids: set[str],
                        assignees: dict[str, str]) -> tuple[str, str | None, str | None]:
        key = (it.trigger.portfolio_id, it.trigger.symbol)
        if last.get(key) in open_ids:
            return ISSUE_OPEN, last[key], None
        try:
            name = it.agent_name or ""
            if name not in assignees:
                assignees[name] = await resolve_agent(client, name)
            try:
                ident = await client.create_issue(
                    title=it.title, description=it.text, assignee_agent_id=assignees[name],
                    priority="high")
            except PaperclipError as e:
                if e.message != "timeout":
                    raise
                # the issue may exist anyway
                ident = await client.open_issue_with_title(it.title)
                if not ident:
                    raise
        except PaperclipError as e:
            return FAILED, None, _short(e.message)
        except Exception:
            log.warning("intraday wake: unexpected error", exc_info=True)
            return FAILED, None, "errore inatteso"
        last[key] = ident
        open_ids.add(ident)
        return WOKEN, ident, None

    # -- step 3: the outcomes, one transaction --------------------------------------------

    async def _write(self, items: list[_Item],
                     results: dict[int, tuple[str, str | None, str | None]]) -> None:
        async with self._sm() as session, session.begin():
            for it in items:
                outcome, ident, error = results[it.row_id]
                done = await session.execute(
                    update(IntradayTrigger)
                    .where(IntradayTrigger.id == it.row_id, IntradayTrigger.outcome == PENDING)
                    .values(outcome=outcome, issue_ident=ident, error=error))
                if outcome == FAILED and done.rowcount:
                    await enqueue(session, source="system", category="system",
                                  level="warning", text=f"{it.title}\n{it.text}")
                    log.warning("intraday wake failed: %s", error)
