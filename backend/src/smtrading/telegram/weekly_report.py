"""«Resoconto del venerdì»: every week, on the set day and time (admin setting
`app_settings["weekly_report_pdf"]` = {"on", "weekday", "at"}, default on, Friday at
22:30 local time: after the US close and the PM's 22:15 cycle), the «Resoconto» PDF of
the week (`reports.period` + `reports.pdf`) is sent to the group as a document, with a
short caption: the week, the total P&L in € and %, whether the weekly goal is reached and
the agents' cost.

- Period: local Monday 00:00 → now.
- Portfolios: those counted in the totals (`total_portfolio_ids`); the other live
  portfolios (`in_totals` false) on a page of their own, clearly labelled, never summed
  with the totals. Archived portfolios are left out.
- The weekly goal: «the weekly goal» (`goals.service.weekly_goal`) when active, its
  progress in the current week (`goals.progress.goal_progress`).

One document per week: claimed in `job_runs` (job `telegram.weekly_report`, key the
local Monday) in the same transaction as its outbox row, so a restart never sends twice.
While Telegram is not ready nothing is claimed; the report still goes out once it is,
within SEND_WINDOW after the set time; later the week is claimed and skipped (WARNING).
A PDF over the outbox limit (20 MB) is replaced by the caption alone, with a note.
Runs behind the worker's startup gate, like the other jobs that write to the owner."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import ActivityLevel
from smtrading.common.config import get_settings
from smtrading.engine.quotesource import QuoteSource
from smtrading.goals.progress import REACHED, goal_progress
from smtrading.goals.service import weekly_goal
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.portfolios.models import Portfolio, live_portfolio_ids, total_portfolio_ids
from smtrading.reports.pdf import _num, pct, render_pdf, signed_money
from smtrading.reports.period import period_report, portfolio_section
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.outbox import MAX_DOCUMENT_BYTES, enqueue, enqueue_document
from smtrading.telegram.weekly_report_settings import load_weekly_report_settings

log = logging.getLogger(__name__)

JOB = "telegram.weekly_report"
TITLE = "Resoconto settimanale"
SEND_WINDOW = timedelta(hours=24)  # after the set time a missed report is not sent any more
TICK_S = 60
CATEGORY = "weekly"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def week_start(local: datetime) -> date:
    """The local Monday of the week of `local`."""
    return local.date() - timedelta(days=local.weekday())


def filename(start: date, end: date) -> str:
    return f"resoconto-settimanale-{start.isoformat()}-{end.isoformat()}.pdf"


async def _ids(session: AsyncSession) -> tuple[list, list]:
    """(the portfolios counted in the totals, the other live ones), by name."""
    inside = list(await session.scalars(
        select(Portfolio.id).where(Portfolio.id.in_(total_portfolio_ids()))
        .order_by(Portfolio.name)))
    outside = list(await session.scalars(
        select(Portfolio.id).where(Portfolio.id.in_(live_portfolio_ids()),
                                   Portfolio.in_totals.is_(False))
        .order_by(Portfolio.name)))
    return inside, outside


async def build_report(session: AsyncSession, qs: QuoteSource, now: datetime,
                       zone: ZoneInfo) -> dict[str, Any]:
    """The week's report data, with `outside_totals` (the portfolios left out of the
    totals) and `weekly_goal` (None, or {"target_pct", "pct", "reached"})."""
    start, end = week_start(now.astimezone(zone)), now.astimezone(zone).date()
    inside, outside = await _ids(session)
    data = await period_report(session, qs, start=start, end=end, portfolio_ids=inside,
                               now=now, zone=zone)
    data["title"] = TITLE
    data["outside_totals"] = (await portfolio_section(
        session, qs, start=start, end=end, portfolio_ids=outside, now=now, zone=zone)
        if outside else None)
    goal = await weekly_goal(session)
    if goal is not None and goal.active:
        p = await goal_progress(session, goal, now, zone)
        data["weekly_goal"] = {"target_pct": goal.target_pct, "pct": p["pct"],
                               "reached": p["status"] == REACHED}
    else:
        data["weekly_goal"] = None
    return data


def caption(data: dict[str, Any]) -> str:
    """The short caption of the document (pure, plain text, well under 1024)."""
    t = data["total"]
    week = f"{data['from']:%d/%m}–{data['to']:%d/%m/%Y}"
    lines = [f"📄 {TITLE} — {week}",
             f"P&L totale: {signed_money(t['change_eur'])} ({pct(t['return_pct'])})"]
    g = data.get("weekly_goal")
    if g is None:
        lines.append("Obiettivo settimanale: nessuno attivo")
    else:
        verdict = "raggiunto ✅" if g["reached"] else "non raggiunto"
        lines.append(f"Obiettivo settimanale +{_num(g['target_pct'], 1)}%: {verdict} "
                     f"(ora {pct(g['pct'])})")
    lines.append(f"Costo agenti: {_num(data['agent_costs']['cost_usd'])} USD")
    outside = (data.get("outside_totals") or {}).get("portfolios") or []
    if outside:
        lines.append(f"Fuori dai totali: {len(outside)} portafogli (pagina a parte)")
    return "\n".join(lines)


class WeeklyReport:
    def __init__(self, sessionmaker: async_sessionmaker, qs: QuoteSource, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 tick_s: float = TICK_S):
        self._sm = sessionmaker
        self._qs = qs
        self._clock = clock
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._tick_s = tick_s
        self._done: str | None = None  # the week (local Monday) known done or given up

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("weekly report failed")
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock()
        local = now.astimezone(self._zone)
        monday = week_start(local)
        key = monday.isoformat()
        if key == self._done:
            return
        async with self._sm() as session:
            settings = await load_weekly_report_settings(session)
            if not settings.on:
                return  # not marked: turned on later the same week it still runs
            due = datetime.combine(monday + timedelta(days=settings.weekday), settings.at,
                                   tzinfo=self._zone)
            if local < due:
                return
            if await job_done(session, JOB, key):
                self._done = key
                return
            ready = is_ready(await load_config(session))
        late = local >= due + SEND_WINDOW
        if late:
            async with self._sm() as session, session.begin():
                if await run_job_once(session, JOB, key):
                    log.warning("%s of the week of %s skipped: not sent within %s of %s",
                                JOB, key, SEND_WINDOW, due.isoformat())
            self._done = key
            return
        if not ready:
            return  # not claimed: it goes out if Telegram is ready within the window
        async with self._sm() as session:
            data = await build_report(session, self._qs, now, self._zone)
        body = await asyncio.to_thread(render_pdf, data)
        text = caption(data)
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            if not is_ready(cfg):
                return  # turned off meanwhile: tried again next tick
            if await run_job_once(session, JOB, key):  # else another worker sent it
                if len(body) <= MAX_DOCUMENT_BYTES:
                    await enqueue_document(
                        session, source="system", category=CATEGORY,
                        level=ActivityLevel.info, caption=text,
                        filename=filename(data["from"], data["to"]), content=body, cfg=cfg)
                else:
                    log.warning("%s: PDF of %s bytes over the Telegram limit", JOB, len(body))
                    await enqueue(
                        session, source="system", category=CATEGORY,
                        level=ActivityLevel.info, cfg=cfg,
                        text=text + f"\n\nIl PDF ({len(body) / 1024 / 1024:.1f} MB) supera il "
                        "limite di 20 MB: scaricalo dal pulsante «Resoconto» del portale.")
        self._done = key
