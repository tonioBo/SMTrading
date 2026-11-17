"""The weekly goal of the old surfaces, now a thin wrapper over the goals
(`smtrading.goals`): «the weekly goal» is the earliest recurring weekly minimum goal on
the total (`goals.service.weekly_goal`, migrated from the former setting
`app_settings["weekly_goal"]`). It backs `GET /api/weekly-goal` (`weekly_goal_view`), the
`weekly_goal` field of the app settings and the 1w goal of the «Andamento» chart; the
Telegram summaries list every goal (`goals.progress.summary_lines`).

The maths is `goals.progress`: start = the Monday `day_open` (else the earliest snapshot
of the week), now = the latest snapshot, over the live portfolios."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.periods import calendar_period
from smtrading.goals.progress import (
    PortfolioPeriod,
    goal_change_pct,
    goal_target,
    portfolio_periods,
    round_cents,
)
from smtrading.goals.service import parse_pct, set_weekly_goal, weekly_goal
from smtrading.portfolios.models import Portfolio

DEFAULT_PCT = Decimal("2.0")
MAX_PCT = Decimal(100)

PortfolioWeek = PortfolioPeriod


@dataclass(frozen=True)
class WeeklyGoal:
    on: bool = False
    pct: Decimal = DEFAULT_PCT


def _pct_text(pct: Decimal) -> str:
    text = format(pct.normalize(), "f")
    return text if "." in text else text + ".0"


async def load_weekly_goal(session: AsyncSession) -> WeeklyGoal:
    """On while «the weekly goal» exists and is active; off at +2% when there is none."""
    goal = await weekly_goal(session)
    return WeeklyGoal() if goal is None else WeeklyGoal(goal.active, goal.target_pct)


def weekly_goal_dto(goal: WeeklyGoal) -> dict[str, Any]:
    """The admin view: {"on": bool, "pct": "2.0"}."""
    return {"on": goal.on, "pct": _pct_text(goal.pct)}


def clean_weekly_goal(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """`current` (a `weekly_goal_dto`) with the fields of `body` validated and applied."""
    out = dict(current)
    if body.get("on") is not None:
        if not isinstance(body["on"], bool):
            raise _field_error("obiettivo settimanale: on deve essere vero o falso")
        out["on"] = body["on"]
    if body.get("pct") is not None:
        try:
            out["pct"] = _pct_text(parse_pct(body["pct"], "weekly_goal", MAX_PCT))
        except SMTError as e:
            raise _field_error(f"obiettivo settimanale: {e.message}") from None
    return out


def _field_error(message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": "weekly_goal"})


async def store_weekly_goal(session: AsyncSession, cleaned: dict[str, Any],
                            today: date) -> None:
    """Writes a `clean_weekly_goal` result onto «the weekly goal»."""
    await set_weekly_goal(session, on=cleaned["on"], pct=Decimal(cleaned["pct"]), today=today)


def week_monday(now: datetime, zone: ZoneInfo) -> date:
    """The local Monday of the week of `now`."""
    day = now.astimezone(zone).date()
    return day - timedelta(days=day.weekday())


async def portfolio_weeks(session: AsyncSession, now: datetime,
                          zone: ZoneInfo) -> list[PortfolioPeriod]:
    """(start, now) of every live portfolio with a snapshot this week."""
    return await portfolio_periods(
        session, calendar_period("week", now.astimezone(zone).date()), now, zone)


async def weekly_goal_view(session: AsyncSession, now: datetime,
                           zone: ZoneInfo) -> dict[str, Any]:
    """The portal's weekly goal: the setting, the total and each portfolio (Decimals
    rounded to cents; the total's fields None while the week has no data)."""
    goal = await load_weekly_goal(session)
    weeks = await portfolio_weeks(session, now, zone)
    ids: list[UUID] = [w.portfolio_id for w in weeks]
    names = dict((await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(ids))
    )).all()) if weeks else {}
    start = sum((w.start for w in weeks), Decimal(0)) if weeks else None
    current = sum((w.current for w in weeks), Decimal(0)) if weeks else None
    return {
        "week_start": week_monday(now, zone),
        "on": goal.on,
        "target_pct": goal.pct,
        "total": {
            "start_eur": round_cents(start), "current_eur": round_cents(current),
            "target_eur": round_cents(goal_target(goal.pct, start)) if weeks else None,
            "pct": round_cents(goal_change_pct(start, current)) if weeks else None,
        },
        "portfolios": sorted((
            {"portfolio_id": w.portfolio_id, "name": names.get(w.portfolio_id),
             "start_eur": round_cents(w.start), "current_eur": round_cents(w.current),
             "target_eur": round_cents(goal_target(goal.pct, w.start)),
             "pct": round_cents(goal_change_pct(w.start, w.current))}
            for w in weeks), key=lambda item: item["name"] or ""),
    }
