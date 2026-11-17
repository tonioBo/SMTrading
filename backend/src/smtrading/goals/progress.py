"""The progress of the goals (the maths of the former `telegram.weekly_goal`, for every
horizon and scope).

The equity of a goal is the sum, over its portfolios (scope total: the live ones; scope
portfolio: that one, unless archived), of the equity in EUR of the `equity_snapshots`:
- start of a portfolio: its `day_open` of the period's first day, else its earliest
  snapshot within the period (a portfolio created later, or no snapshot at the very
  start);
- current: its latest snapshot up to now (or up to the end of a period already over).
A portfolio without a snapshot in the period is left out of both sums. Nothing is valued
here (the worker writes a snapshot every 5 minutes).

Status (`status_of`): «non iniziato» before the period or without data yet; kind min:
«raggiunto» once pct ≥ target, «in ritardo» while pct < target × elapsed − 0,5 pp,
else «in linea»; kind cap: «oltre il tetto» once pct > target, else «in linea». A period
over (goals that do not recur) is «raggiunto» / «oltre il tetto» as above, else
«concluso».

Every figure leaves `goal_progress` as a Decimal rounded to cents (None without data); the
routes turn them into strings."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.goals.models import HORIZONS, Goal
from smtrading.goals.periods import Period, elapsed_pct, goal_period, previous_period
from smtrading.goals.service import portfolio_labels
from smtrading.jobs.runs import run_job_once
from smtrading.portfolios.models import Portfolio, live_portfolio_ids
from smtrading.risk.models import EquitySnapshot, SnapshotKind

PERIOD_END_JOB = "goals.period_end"
END_LOOKBACK = timedelta(days=7)  # a period end not announced within a week is dropped
BEHIND_MARGIN_PP = Decimal("0.5")
_Q2 = Decimal("0.01")

REACHED, ON_TRACK, BEHIND, NOT_STARTED, OVER, ABOVE_CAP = (
    "raggiunto", "in linea", "in ritardo", "non iniziato", "concluso", "oltre il tetto")
STATUSES = (REACHED, ON_TRACK, BEHIND, NOT_STARTED, OVER, ABOVE_CAP)


# -- pure maths ------------------------------------------------------------------------


def goal_target(pct: Decimal, start: Decimal) -> Decimal:
    """The equity to reach: `start` grown by `pct` percent."""
    return start * (1 + pct / 100)


def goal_change_pct(start: Decimal, current: Decimal) -> Decimal | None:
    """The change in percent, or None without a positive starting equity."""
    if start <= 0:
        return None
    return (current / start - 1) * 100


def status_of(kind: str, pct: Decimal | None, target: Decimal, elapsed: Decimal, *,
              started: bool, ended: bool) -> str:
    """The status of a goal (see the module docstring); `elapsed` in percent."""
    if not started or pct is None:
        return OVER if ended and started else NOT_STARTED
    if kind == "cap":
        if pct > target:
            return ABOVE_CAP
        return OVER if ended else ON_TRACK
    if pct >= target:
        return REACHED
    if ended:
        return OVER
    if pct < target * elapsed / 100 - BEHIND_MARGIN_PP:
        return BEHIND
    return ON_TRACK


def round_cents(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_Q2, rounding=ROUND_HALF_EVEN)


# -- snapshots -------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioPeriod:
    portfolio_id: UUID
    start: Decimal
    current: Decimal
    as_of: datetime  # ts of the snapshot giving `current`


def local_midnight(day: date, zone: ZoneInfo) -> datetime:
    """00:00 local of `day`, in UTC."""
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


async def portfolio_periods(session: AsyncSession, period: Period, now: datetime,
                            zone: ZoneInfo,
                            portfolio_ids: list[UUID] | None = None) -> list[PortfolioPeriod]:
    """(start, current) of every live portfolio (or of the live ones of `portfolio_ids`)
    with a snapshot in `period` up to `now`."""
    start_ts = local_midnight(period.start, zone)
    end_ts = local_midnight(period.end + timedelta(days=1), zone)
    if now < start_ts:
        return []
    ids_stmt = live_portfolio_ids()
    if portfolio_ids is not None:
        ids_stmt = ids_stmt.where(Portfolio.id.in_(portfolio_ids))
    out = []
    for pid in list(await session.scalars(ids_stmt.order_by(Portfolio.id))):
        in_period = (select(EquitySnapshot.equity_eur, EquitySnapshot.ts)
                     .where(EquitySnapshot.portfolio_id == pid, EquitySnapshot.ts <= now,
                            EquitySnapshot.ts < end_ts))
        start = await session.scalar(
            select(EquitySnapshot.equity_eur)
            .where(EquitySnapshot.portfolio_id == pid,
                   EquitySnapshot.kind == SnapshotKind.day_open,
                   EquitySnapshot.local_date == period.start, EquitySnapshot.ts <= now))
        if start is None:
            start = (await session.execute(
                in_period.where(EquitySnapshot.ts >= start_ts)
                .order_by(EquitySnapshot.ts, EquitySnapshot.id).limit(1))).scalar()
        if start is None:
            continue
        row = (await session.execute(
            in_period.order_by(EquitySnapshot.ts.desc(), EquitySnapshot.id.desc())
            .limit(1))).first()
        if row is None:  # only a day_open stamped before its period (never in practice)
            continue
        out.append(PortfolioPeriod(pid, start, row[0], row[1]))
    return out


# -- progress --------------------------------------------------------------------------


async def period_progress(session: AsyncSession, goal: Goal, period: Period, now: datetime,
                          zone: ZoneInfo, portfolio_name: str | None) -> dict[str, Any]:
    """The progress of `goal` over `period` at `now` (the Progress of the contract, with
    Decimals, dates and datetimes)."""
    local = now.astimezone(zone)
    today = local.date()
    weeks = await portfolio_periods(
        session, period, now, zone,
        None if goal.scope == "total" else [goal.portfolio_id])
    target = goal.target_pct
    start = sum((w.start for w in weeks), Decimal(0)) if weeks else None
    current = sum((w.current for w in weeks), Decimal(0)) if weeks else None
    pct = goal_change_pct(start, current) if weeks else None
    target_eur = goal_target(target, start) if weeks else None
    elapsed = elapsed_pct(period, local)
    return {
        "goal_id": goal.id, "name": goal.name, "horizon": goal.horizon, "scope": goal.scope,
        "portfolio_id": goal.portfolio_id, "portfolio_name": portfolio_name,
        "kind": goal.kind, "period_start": period.start, "period_end": period.end,
        "start_eur": round_cents(start), "current_eur": round_cents(current),
        "target_eur": round_cents(target_eur), "pct": round_cents(pct), "target_pct": target,
        "gap_eur": round_cents(target_eur - current) if weeks else None,
        "elapsed_pct": elapsed,
        "status": status_of(goal.kind, pct, target, elapsed,
                            started=today >= period.start, ended=today > period.end),
        "as_of": max(w.as_of for w in weeks) if weeks else None,
    }


def _order(goal: Goal) -> tuple:
    return (HORIZONS.index(goal.horizon), goal.scope != "total", goal.name.casefold(),
            goal.created_at or datetime.min.replace(tzinfo=UTC), str(goal.id))


async def visible_goals(session: AsyncSession, *, portfolio_ids: set[UUID] | None = None
                        ) -> list[tuple[Goal, str | None]]:
    """The active goals with the name of their portfolio: the total ones and the ones of
    live portfolios (of `portfolio_ids` only, when given), in display order."""
    goals = list(await session.scalars(select(Goal).where(Goal.active.is_(True))))
    names = await portfolio_labels(session, {g.portfolio_id for g in goals if g.portfolio_id})
    out = []
    for g in goals:
        if g.scope == "portfolio":
            name, archived = names.get(g.portfolio_id, (None, True))
            if archived or (portfolio_ids is not None and g.portfolio_id not in portfolio_ids):
                continue
            out.append((g, name))
        else:
            out.append((g, None))
    return sorted(out, key=lambda item: _order(item[0]))


def current_period(goal: Goal, today: date) -> Period:
    return goal_period(goal.horizon, goal.start_date, goal.end_date, goal.recurring, today)


async def goal_progress(session: AsyncSession, goal: Goal, now: datetime, zone: ZoneInfo,
                        portfolio_name: str | None = None) -> dict[str, Any]:
    """The progress of `goal` in its current period."""
    period = current_period(goal, now.astimezone(zone).date())
    return await period_progress(session, goal, period, now, zone, portfolio_name)


async def active_progress(session: AsyncSession, now: datetime, zone: ZoneInfo, *,
                          portfolio_ids: set[UUID] | None = None) -> list[dict[str, Any]]:
    """The progress of the active goals in their current period (`visible_goals`)."""
    return [await goal_progress(session, g, now, zone, name)
            for g, name in await visible_goals(session, portfolio_ids=portfolio_ids)]


# -- Telegram lines --------------------------------------------------------------------


def _number(value: Decimal) -> str:
    """Italian digits with 2 decimals, no sign: `1.004,20`."""
    q = abs(value.quantize(_Q2, rounding=ROUND_HALF_EVEN))
    return f"{q:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _signed(value: Decimal) -> str:
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return ("+" if q > 0 else "-" if q < 0 else "") + _number(q)


def _goal_pct(pct: Decimal) -> str:
    return "+" + format(pct.normalize(), "f").replace(".", ",") + "%"


def _head(p: dict[str, Any]) -> str:
    head = f"Obiettivo {p['name']} {_goal_pct(p['target_pct'])}"
    if p["kind"] == "cap":
        head += " (tetto)"
    if p["portfolio_name"]:
        head += f" · {p['portfolio_name']}"
    return head


def progress_line(p: dict[str, Any], today: date) -> str:
    """«🎯 Obiettivo settimanale +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 €
    (+0,42%) · in linea» (pure, from a `period_progress`); before the period «dal
    <date>», without data «dati del periodo non ancora disponibili»."""
    head = f"🎯 {_head(p)}"
    if today < p["period_start"]:
        return f"{head}: dal {p['period_start']:%d/%m/%Y}"
    if p["pct"] is None:
        return f"{head}: dati del periodo non ancora disponibili"
    return (f"{head}: {_number(p['start_eur'])} → {_number(p['target_eur'])} € — attuale "
            f"{_number(p['current_eur'])} € ({_signed(p['pct'])}%) · {p['status']}")


def end_line(p: dict[str, Any]) -> str:
    """«🏁 Obiettivo settimanale: raggiunto (+2,10% su +2%)» / «non raggiunto (…)» for a
    period just over (pure); kind cap: «tetto rispettato» / «tetto superato»; without
    data «nessun dato»."""
    name = p["name"] + (f" · {p['portfolio_name']}" if p["portfolio_name"] else "")
    target = _goal_pct(p["target_pct"])
    if p["pct"] is None:
        return f"🏁 Obiettivo {name}: nessun dato del periodo (obiettivo {target})"
    if p["kind"] == "cap":
        word = "tetto rispettato" if p["pct"] <= p["target_pct"] else "tetto superato"
    else:
        word = "raggiunto" if p["pct"] >= p["target_pct"] else "non raggiunto"
    return f"🏁 Obiettivo {name}: {word} ({_signed(p['pct'])}% su {target})"


async def summary_lines(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[str]:
    """One line per active goal whose period is not over (the summaries, the pre-open
    check); a period not started yet reads «dal <date>»."""
    today = now.astimezone(zone).date()
    out = []
    for p in await active_progress(session, now, zone):
        if today <= p["period_end"]:
            out.append(progress_line(p, today))
    return out


def _ending_periods(goal: Goal, today: date) -> list[Period]:
    period = current_period(goal, today)
    candidates = [period]
    if goal.recurring:
        candidates.append(previous_period(goal.horizon, period))
    return [c for c in candidates if c.end <= today and c.end >= goal.start_date
            and today - END_LOOKBACK <= c.end]


async def period_end_lines(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[str]:
    """«🏁 …» once per goal and period over: a period ends with its last day, so the
    daily summary of that day (or of a later one within END_LOOKBACK) carries it. Each
    is claimed in `job_runs` (`goals.period_end`) in the caller's transaction. Periods
    that ended before the goal was created, or without data, are never announced."""
    today = now.astimezone(zone).date()
    out = []
    for goal, name in await visible_goals(session):
        created = (goal.created_at or now).astimezone(zone).date()
        for period in _ending_periods(goal, today):
            if period.end < created:
                continue
            key = f"{goal.id}:{period.start.isoformat()}:{period.end.isoformat()}"
            if not await run_job_once(session, PERIOD_END_JOB, key):
                continue
            progress = await period_progress(session, goal, period, now, zone, name)
            if progress["pct"] is not None:  # a period without data is not announced
                out.append(end_line(progress))
    return out
