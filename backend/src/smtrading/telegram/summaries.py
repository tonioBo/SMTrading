"""Scheduled Telegram summaries, built by SMTrading itself (they arrive even when Paperclip
is down): the night digest at 07:00, the daily summary at 18:00 and the weekly one on
Friday at 18:30, local time (`get_settings().tz`).

Each is claimed once per local date in `job_runs` (jobs `telegram.digest`,
`telegram.daily`, `telegram.weekly`), in the same transaction as its outbox rows, so a
restart never sends twice. A summary is sent only while Telegram is ready; a worker that
was down at the scheduled time sends it when it comes back, the same day before 23:00.
Later it is skipped (WARNING, marked done): past days are never recovered. When the 23:00
cutoff passes while Telegram is not ready, one WARNING per summary and day is logged and
nothing is claimed.

One rule for archived portfolios: they are left out of the digest and of the summaries
(their digest rows are marked `skipped`). The daily and weekly summaries count only the
portfolios counted in the total (`total_portfolio_ids`: live and `in_totals`), like
their total value: a hidden test portfolio's orders, events, decisions, positions and
ranking row are left out of every count and list (found on 27/11: 73 executed orders,
Prova B's positions).

The ranking uses `get_performance` (same return and drawdown as the API) over the
portfolios counted in the total, plus the fills of the period.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity, ActivityLevel, Decision, TelegramState
from smtrading.agents.models import Agent
from smtrading.common.config import get_settings
from smtrading.engine.models import Fill
from smtrading.goals.progress import period_end_lines, summary_lines
from smtrading.jobs.runs import run_job_once
from smtrading.performance.service import get_performance
from smtrading.portfolios.models import (
    Portfolio,
    PortfolioStatus,
    total_portfolio_ids,
)
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.details import open_positions_lines
from smtrading.telegram.outbox import enqueue
from smtrading.telegram.quiet import QUIET_START
from smtrading.telegram.render import PREFIX, split_message
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

Kind = Literal["daily", "weekly"]
Parts = list[tuple[str, list[dict] | None]]

TICK_S = 30
NAME_WIDTH = 18
SECTION_LINES = 20  # lines listed per summary section, then "… e altri N"
CUTOFF = QUIET_START  # a summary not sent by 23:00 of its day is skipped


@dataclass(frozen=True)
class Job:
    name: str
    category: str
    at: time
    weekday: int | None = None  # Monday = 0


JOBS = (
    Job("telegram.digest", "digest", time(7)),
    Job("telegram.daily", "daily", time(18)),
    Job("telegram.weekly", "weekly", time(18, 30), weekday=4),
)

DECISION_GROUPS = (
    (("strategy",), "Strategie"), (("risk_rule",), "Rischio"),
    (("allocation",), "Allocazioni"), (("veto", "approval", "proposal"), "Veti e approvazioni"),
    (("team_rule", "lesson"), "Lezioni e regole del team"), (("other",), "Altro"),
)
OUTCOME_ICON = {"approvata": "✅", "respinta": "❌", "sospesa": "⏸", "modificata": "✏️",
                "informativa": "ℹ️"}
_KIND_ICON = {"approval": "✅", "veto": "❌"}  # decisions without an outcome
DECISION_LINES = 15  # decision lines per summary, then "… e altre N"
SUMMARY_SUBJECT = 120  # a summary used as the subject is cut to this length
REF_MAX = 20
_INTERNAL_IDS = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|#\d+", re.IGNORECASE)
_DIGEST_GROUPS = (
    (ActivityLevel.critical, "Eventi critici"), (ActivityLevel.warning, "Avvisi"),
    (ActivityLevel.executed, "Ordini eseguiti"), (ActivityLevel.info, "Altri eventi"),
)
_Q2 = Decimal("0.01")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# -- ranking -------------------------------------------------------------------------


@dataclass
class RankRow:
    portfolio: str
    return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    trades: int


async def ranking(session: AsyncSession, *, period: Literal["1d", "1w"], since: datetime,
                  now: datetime) -> list[RankRow]:
    """Every portfolio counted in the total (`total_portfolio_ids`): return and max
    drawdown of `period` (from `get_performance`), fills since `since`. Best return
    first, None last, then by name."""
    portfolios = (await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(total_portfolio_ids()))
    )).all()
    trades = dict((await session.execute(
        select(Fill.portfolio_id, func.count())
        .where(Fill.created_at >= since, Fill.created_at <= now,
               Fill.portfolio_id.in_(total_portfolio_ids()))
        .group_by(Fill.portfolio_id)
    )).all())
    rows = []
    for pid, name in portfolios:
        perf = await get_performance(session, pid, period, now=now)
        rows.append(RankRow(name, perf["return_pct"], perf["max_drawdown_pct"],
                            trades.get(pid, 0)))
    rows.sort(key=lambda r: r.portfolio)
    rows.sort(key=lambda r: (r.return_pct is None, -(r.return_pct or 0)))
    return rows


def format_pct(value: Decimal | None) -> str:
    """Italian format with 2 decimals and a sign: `+1,23`, `-0,50`, `0,00`, `—`."""
    if value is None:
        return "—"
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    sign = "+" if q > 0 else "-" if q < 0 else ""
    digits = f"{abs(q):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return sign + digits


def _one_line(name: str) -> str:
    return " ".join(name.split())


def ranking_table(rows: list[RankRow]) -> str:
    """A fixed-width table for a `pre` block (pure)."""
    def line(name: str, ret: str, dd: str, ops: str) -> str:
        return f"{name:<{NAME_WIDTH}} {ret:>9} {dd:>9} {ops:>4}"

    out = [line("Portafoglio", "Rend. %", "Max DD %", "Op.")]
    for r in rows:
        out.append(line(_one_line(r.portfolio)[:NAME_WIDTH], format_pct(r.return_pct),
                        format_pct(r.max_drawdown_pct), str(r.trades)))
    if not rows:
        out.append("(nessun portafoglio attivo)")
    return "\n".join(out)


# -- message bodies --------------------------------------------------------------------


async def _portfolio_names(session: AsyncSession, rows: list[Activity]) -> dict:
    ids = {a.portfolio_id for a in rows if a.portfolio_id}
    if not ids:
        return {}
    return dict((await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(ids)))).all())


def _event_line(a: Activity, names: dict[uuid.UUID, str], zone: ZoneInfo, fmt: str) -> str:
    line = f"{a.ts.astimezone(zone).strftime(fmt)} {_one_line(a.summary)}"
    name = names.get(a.portfolio_id) if a.portfolio_id else None
    return f"{line} — {_one_line(name)}" if name else line


def _section(title: str, lines: list[str], limit: int | None = SECTION_LINES) -> list[str]:
    if not lines:
        return []
    shown = lines if limit is None else lines[:limit]
    out = ["", f"{title} ({len(lines)})", *shown]
    if len(lines) > len(shown):
        out.append(f"… e altri {len(lines) - len(shown)}")
    return out


def _plain(text: str | None) -> str:
    """One line, without internal ids (uuids, `#<number>`)."""
    return _one_line(_INTERNAL_IDS.sub("", text or ""))


def decision_line(d: Decision, agent: str, portfolio: str | None) -> str:
    """One decision for people (pure): `✅ STR-001 · <subject> — <portfolio> — <reason>
    (<agent>)`. Without a subject the summary, cut to 120 characters; without a reason
    that segment is left out; internal ids never appear."""
    icon = OUTCOME_ICON.get(d.outcome or "") or _KIND_ICON.get(d.kind, "•")
    ref = (d.details or {}).get("ref")
    ref = _one_line(ref) if isinstance(ref, str) and len(ref) <= REF_MAX else ""
    subject = _plain(d.subject)
    if not subject:
        subject = _plain(d.summary)
        if len(subject) > SUMMARY_SUBJECT:
            subject = subject[:SUMMARY_SUBJECT - 1].rstrip() + "…"
    reason = _plain(d.reason)
    where = _one_line(portfolio) if portfolio else "generale"
    return (f"{icon} {ref + ' · ' if ref else ''}{subject} — {where}"
            f"{' — ' + reason if reason else ''} ({_one_line(agent)})")


REMOVED_AGENT = "Agente rimosso"


async def _decision_section(session: AsyncSession, kind: Kind, since: datetime,
                            now: datetime, zone: ZoneInfo) -> list[str]:
    """«🧭 Decisioni di oggi (N)» (or «della settimana»), grouped by kind, decisions of
    portfolios not counted in the total (archived, `in_totals` false) left out; at most 15 lines, then «… e altre N». A decision whose
    agent row is gone is still listed, by «Agente rimosso»."""
    rows = (await session.execute(
        select(Decision, func.coalesce(Agent.name, REMOVED_AGENT), Portfolio.name)
        .outerjoin(Agent, Agent.id == Decision.agent_id)
        .outerjoin(Portfolio, Portfolio.id == Decision.portfolio_id)
        .where(Decision.created_at >= since, Decision.created_at <= now,
               or_(Decision.portfolio_id.is_(None),
                   Decision.portfolio_id.in_(total_portfolio_ids())))
        .order_by(Decision.created_at, Decision.id)
    )).all()
    label = "di oggi" if kind == "daily" else "della settimana"
    if not rows:
        return [f"Decisioni {label}: nessuna"]
    out = [f"🧭 Decisioni {label} ({len(rows)})"]
    shown = 0
    for kinds, title in DECISION_GROUPS:
        group = [r for r in rows if r[0].kind in kinds]
        if not group or shown >= DECISION_LINES:
            continue
        out.append(title)
        for d, agent, portfolio in group[:DECISION_LINES - shown]:
            line = decision_line(d, agent, portfolio)
            if kind == "weekly":
                line = f"{d.created_at.astimezone(zone):%d/%m} {line}"
            out.append(line)
            shown += 1
    if len(rows) > shown:
        out.append(f"… e altre {len(rows) - shown}")
    return out


async def build_digest(session: AsyncSession, now: datetime, *,
                       tz: str | None = None) -> Parts | None:
    """The night digest of the activity rows marked `digest`, or None when there are none.
    Rows of portfolios archived meanwhile are marked `skipped` and left out. Queues its messages (category `digest`) and marks the rows `queued` with the outbox id
    of the first message, in the caller's transaction; returns the queued parts."""
    zone = ZoneInfo(tz or get_settings().tz)
    rows = list(await session.scalars(
        select(Activity).where(Activity.telegram == TelegramState.digest)
        .order_by(Activity.id).with_for_update(skip_locked=True)
    ))
    archived = set(await session.scalars(
        select(Portfolio.id).where(
            Portfolio.id.in_({a.portfolio_id for a in rows if a.portfolio_id}),
            Portfolio.status == PortfolioStatus.archived)
    )) if rows else set()
    for a in rows:
        if a.portfolio_id in archived:  # the same rule as the summaries: left out
            a.telegram = TelegramState.skipped
    rows = [a for a in rows if a.portfolio_id not in archived]
    if not rows:
        return None
    names = await _portfolio_names(session, rows)
    lines = ["🌙 Riepilogo della notte"]
    for level, title in _DIGEST_GROUPS:
        group = [_event_line(a, names, zone, "%H:%M") for a in rows if a.level == level]
        lines += _section(f"{PREFIX[level]}{title}", group, limit=None)
    parts = split_message("\n".join(lines))
    first = None
    for text, entities in parts:
        msg = await enqueue(session, source="system", category="digest",
                            level=ActivityLevel.info, text=text, entities=entities)
        first = first or msg
    for a in rows:
        a.telegram = TelegramState.queued
        a.outbox_id = first.id if first else None
    return parts


def _window(kind: Kind, now: datetime, zone: ZoneInfo) -> datetime:
    if kind == "daily":
        midnight = datetime.combine(now.astimezone(zone).date(), time(0), tzinfo=zone)
        return midnight.astimezone(UTC)
    return now - timedelta(days=7)


async def build_summary(session: AsyncSession, kind: Kind, now: datetime, *,
                        tz: str | None = None, qs=None, redis=None,
                        period_ends: bool = False) -> Parts:
    """The daily (from local midnight) or weekly (last 7 days) summary: executed and
    rejected orders, critical and warning events, one «🎯» line per active goal
    (`goals.progress.summary_lines`), with `period_ends` (the scheduler's daily) the «🏁»
    lines of the goal periods just over (`period_end_lines`, each claimed once in this
    transaction), «📌 Posizioni aperte» (with a quote source `qs`: one line per open
    position, see `details.open_positions_lines`; nothing when none), the decisions of the period grouped by
    kind (one plain line each, see `decision_line`) and the portfolio
    ranking as a `pre` block, split into messages of at most 4096 characters. Only the
    portfolios counted in the total (`total_portfolio_ids`) are counted and listed."""
    zone = ZoneInfo(tz or get_settings().tz)
    since = _window(kind, now, zone)
    live = or_(Activity.portfolio_id.is_(None),
               Activity.portfolio_id.in_(total_portfolio_ids()))
    rows = list(await session.scalars(
        select(Activity)
        .where(Activity.ts >= since, Activity.ts <= now, live,
               or_(Activity.type.in_(("order.filled", "order.rejected")),
                   Activity.level.in_((ActivityLevel.critical, ActivityLevel.warning))))
        .order_by(Activity.ts, Activity.id)
    ))
    names = await _portfolio_names(session, rows)
    fmt = "%H:%M" if kind == "daily" else "%d/%m %H:%M"

    def lines_of(pred) -> list[str]:
        return [_event_line(a, names, zone, fmt) for a in rows if pred(a)]

    filled = lines_of(lambda a: a.type == "order.filled")
    rejected = lines_of(lambda a: a.type == "order.rejected")
    critical = lines_of(lambda a: a.level == ActivityLevel.critical
                        and a.type not in ("order.filled", "order.rejected"))
    warnings = lines_of(lambda a: a.level == ActivityLevel.warning
                        and a.type not in ("order.filled", "order.rejected"))

    today = now.astimezone(zone)
    if kind == "daily":
        head = f"📊 Riepilogo giornaliero — {today:%d/%m/%Y}"
    else:
        head = (f"📈 Riepilogo settimanale — {since.astimezone(zone):%d/%m}"
                f"–{today:%d/%m/%Y}")
    lines = [
        head,
        (f"Ordini eseguiti: {len(filled)} · rifiutati: {len(rejected)} · "
         f"eventi critici: {len(critical)} · avvisi: {len(warnings)}"),
    ]
    lines += await summary_lines(session, now, zone)
    if period_ends:
        lines += await period_end_lines(session, now, zone)
    if qs is not None:
        lines += _section("📌 Posizioni aperte",
                          await open_positions_lines(session, qs, redis=redis,
                                                     ids=total_portfolio_ids()))
    lines += _section(f"{PREFIX['executed']}Ordini eseguiti", filled)
    lines += _section(f"{PREFIX['warning']}Ordini rifiutati", rejected)
    lines += _section(f"{PREFIX['critical']}Eventi critici", critical)
    lines += _section(f"{PREFIX['warning']}Avvisi", warnings)
    lines.append("")
    lines += await _decision_section(session, kind, since, now, zone)
    period = "1d" if kind == "daily" else "1w"
    lines += ["", "Classifica: rendimento e drawdown delle ultime 24 ore, operazioni di oggi"
              if kind == "daily" else "Classifica degli ultimi 7 giorni"]
    table = ranking_table(await ranking(session, period=period, since=since, now=now))
    return split_message("\n".join(lines), table)


# -- scheduler -------------------------------------------------------------------------


class SummaryScheduler:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 tick_s: float = TICK_S, qs=None, redis=None):
        self._sm = sessionmaker
        self._qs = qs  # None: the summaries go out without «Posizioni aperte»
        self._redis = redis
        self._clock = clock
        self._tz = tz or get_settings().tz
        self._tick_s = tick_s
        self._done: set[tuple[str, str]] = set()  # (job, local date) known done

    async def run(self) -> None:
        while True:
            await self.tick()
            await job_tick()  # «Salute del sistema»: last pass
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock()
        local = now.astimezone(ZoneInfo(self._tz))
        for job in JOBS:
            if job.weekday is not None and local.weekday() != job.weekday:
                continue
            if local.time() < job.at:
                continue
            try:
                await self._run(job, now, local)
            except Exception:
                log.exception("%s failed", job.name)

    async def _run(self, job: Job, now: datetime, local: datetime) -> None:
        key = local.date().isoformat()
        if (job.name, key) in self._done:
            return
        async with self._sm() as session, session.begin():
            if not is_ready(await load_config(session)):
                if local.time() >= CUTOFF:
                    log.warning("%s of %s not sent: Telegram not ready before %s", job.name,
                                key, CUTOFF.strftime("%H:%M"))
                    self._done.add((job.name, key))  # once per summary and day
                return  # not claimed: it goes out if Telegram is ready before 23:00
            claimed = await run_job_once(session, job.name, key)
            if claimed and local.time() >= CUTOFF:
                log.warning("%s of %s skipped: not sent before %s", job.name, key,
                            CUTOFF.strftime("%H:%M"))
            elif claimed:
                await self._send(session, job, now)
        self._done.add((job.name, key))

    async def _send(self, session: AsyncSession, job: Job, now: datetime) -> None:
        if job.category == "digest":
            await build_digest(session, now, tz=self._tz)
            return
        for text, entities in await build_summary(
                session, job.category, now, tz=self._tz, qs=self._qs, redis=self._redis,
                period_ends=job.category == "daily"):
            await enqueue(session, source="system", category=job.category,
                          level=ActivityLevel.info, text=text, entities=entities)
