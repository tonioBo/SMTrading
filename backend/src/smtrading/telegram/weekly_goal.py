"""The weekly goal of the 18:00 daily and of the weekly summary (admin setting
«Obiettivo settimanale», `app_settings["weekly_goal"]` = {"on", "pct"}, default on, +2%).

The goal is measured on the sum of the equity in EUR of the portfolios that are not
archived, from the start of the local week (Monday):
- start of a portfolio: its Monday `day_open` snapshot, else its earliest snapshot of the
  week (a portfolio created mid-week);
- now: its latest snapshot (every 5 minutes, so no quote is fetched here).
A portfolio without a snapshot this week is left out of both sums. The line goes only to
Telegram (the summaries) and the same figures to the portal home (`weekly_goal_view`,
`GET /api/weekly-goal`), never to the agents."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio, live_portfolio_ids
from smtrading.risk.models import AppSetting, EquitySnapshot, SnapshotKind

SETTINGS_KEY = "weekly_goal"
DEFAULT_PCT = Decimal("2.0")
MAX_PCT = Decimal(100)
_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class WeeklyGoal:
    on: bool = True
    pct: Decimal = DEFAULT_PCT


def _invalid(message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": "weekly_goal"})


def _pct(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _invalid("obiettivo settimanale: percentuale non valida")
    try:
        pct = Decimal(str(value).strip())
    except InvalidOperation:
        raise _invalid("obiettivo settimanale: percentuale non valida") from None
    if not pct.is_finite() or not 0 < pct <= MAX_PCT:
        raise _invalid(f"obiettivo settimanale: percentuale tra 0 e {MAX_PCT}")
    if pct != pct.quantize(_Q2):
        raise _invalid("obiettivo settimanale: al massimo 2 decimali")
    return pct


def _stored(raw: Any) -> WeeklyGoal:
    if not isinstance(raw, dict):
        return WeeklyGoal()
    try:
        pct = _pct(raw.get("pct", str(DEFAULT_PCT)))
    except SMTError:
        pct = DEFAULT_PCT
    return WeeklyGoal(on=bool(raw.get("on", True)), pct=pct)


async def load_weekly_goal(session: AsyncSession) -> WeeklyGoal:
    return _stored(await session.scalar(
        select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY)))


def weekly_goal_dto(raw: Any) -> dict[str, Any]:
    """The admin view of the stored value: {"on": bool, "pct": "2.0"}."""
    goal = _stored(raw)
    return {"on": goal.on, "pct": str(goal.pct)}


def clean_weekly_goal(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """`current` (a `weekly_goal_dto`) with the fields of `body` validated and applied."""
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise _invalid("obiettivo settimanale: on deve essere vero o falso")
        out["on"] = body["on"]
    if body.get("pct") is not None:
        out["pct"] = str(_pct(body["pct"]))
    return out


# -- the line --------------------------------------------------------------------------


def _number(value: Decimal) -> str:
    """Italian digits with 2 decimals, no sign: `1.004,20`."""
    q = abs(value.quantize(_Q2, rounding=ROUND_HALF_EVEN))
    return f"{q:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _signed(value: Decimal) -> str:
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return ("+" if q > 0 else "-" if q < 0 else "") + _number(q)


def _goal_pct(pct: Decimal) -> str:
    return "+" + format(pct.normalize(), "f").replace(".", ",") + "%"


def goal_target(pct: Decimal, start: Decimal) -> Decimal:
    """The equity to reach this week: `start` grown by `pct` percent."""
    return start * (1 + pct / 100)


def goal_change_pct(start: Decimal, current: Decimal) -> Decimal | None:
    """The change of the week in percent, or None without a positive starting equity."""
    if start <= 0:
        return None
    return (current / start - 1) * 100


def goal_line(pct: Decimal, start: Decimal | None, current: Decimal | None) -> str:
    """«🎯 Obiettivo settimana +2%: 1.000,00 → 1.020,00 € — attuale 1.004,20 € (+0,42%)»
    (pure); without a positive starting equity, «dati … non ancora disponibili»."""
    head = f"🎯 Obiettivo settimana {_goal_pct(pct)}"
    change = None if start is None or current is None else goal_change_pct(start, current)
    if change is None:
        return f"{head}: dati della settimana non ancora disponibili"
    return (f"{head}: {_number(start)} → {_number(goal_target(pct, start))} € — attuale "
            f"{_number(current)} € ({_signed(change)}%)")


@dataclass(frozen=True)
class PortfolioWeek:
    portfolio_id: UUID
    start: Decimal
    current: Decimal


def week_monday(now: datetime, zone: ZoneInfo) -> date:
    """The local Monday of the week of `now`."""
    day = now.astimezone(zone).date()
    return day - timedelta(days=day.weekday())


async def portfolio_weeks(session: AsyncSession, now: datetime,
                          zone: ZoneInfo) -> list[PortfolioWeek]:
    """(start, now) of every live portfolio with a snapshot this week."""
    monday = week_monday(now, zone)
    week_start = datetime.combine(monday, time(0), tzinfo=zone).astimezone(UTC)
    out = []
    for pid in list(await session.scalars(live_portfolio_ids())):
        of = select(EquitySnapshot.equity_eur).where(EquitySnapshot.portfolio_id == pid)
        start = await session.scalar(of.where(
            EquitySnapshot.kind == SnapshotKind.day_open, EquitySnapshot.local_date == monday))
        if start is None:
            start = await session.scalar(
                of.where(EquitySnapshot.ts >= week_start, EquitySnapshot.ts <= now)
                .order_by(EquitySnapshot.ts, EquitySnapshot.id).limit(1))
        if start is None:
            continue
        current = await session.scalar(
            of.where(EquitySnapshot.ts <= now)
            .order_by(EquitySnapshot.ts.desc(), EquitySnapshot.id.desc()).limit(1))
        out.append(PortfolioWeek(pid, start, current if current is not None else start))
    return out


async def week_equity(session: AsyncSession, now: datetime,
                      zone: ZoneInfo) -> tuple[Decimal, Decimal] | None:
    """(start, now) summed over the portfolios with a snapshot this week, or None."""
    weeks = await portfolio_weeks(session, now, zone)
    if not weeks:
        return None
    return (sum((w.start for w in weeks), Decimal(0)),
            sum((w.current for w in weeks), Decimal(0)))


async def goal_lines(session: AsyncSession, now: datetime, zone: ZoneInfo) -> list[str]:
    """The goal line of a summary, or nothing while the goal is off."""
    goal = await load_weekly_goal(session)
    if not goal.on:
        return []
    totals = await week_equity(session, now, zone)
    return [goal_line(goal.pct, *(totals or (None, None)))]


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_Q2, rounding=ROUND_HALF_EVEN)


async def weekly_goal_view(session: AsyncSession, now: datetime,
                           zone: ZoneInfo) -> dict[str, Any]:
    """The portal's weekly goal: the setting, the total and each portfolio (Decimals
    rounded to cents; the total's fields None while the week has no data)."""
    goal = await load_weekly_goal(session)
    weeks = await portfolio_weeks(session, now, zone)
    names = dict((await session.execute(
        select(Portfolio.id, Portfolio.name)
        .where(Portfolio.id.in_([w.portfolio_id for w in weeks]))
    )).all()) if weeks else {}
    start = sum((w.start for w in weeks), Decimal(0)) if weeks else None
    current = sum((w.current for w in weeks), Decimal(0)) if weeks else None
    change = goal_change_pct(start, current) if weeks else None
    return {
        "week_start": week_monday(now, zone),
        "on": goal.on,
        "target_pct": goal.pct,
        "total": {
            "start_eur": _money(start), "current_eur": _money(current),
            "target_eur": _money(goal_target(goal.pct, start)) if weeks else None,
            "pct": _money(change),
        },
        "portfolios": sorted((
            {"portfolio_id": w.portfolio_id, "name": names.get(w.portfolio_id),
             "start_eur": _money(w.start), "current_eur": _money(w.current),
             "target_eur": _money(goal_target(goal.pct, w.start)),
             "pct": _money(goal_change_pct(w.start, w.current))}
            for w in weeks), key=lambda item: item["name"] or ""),
    }
