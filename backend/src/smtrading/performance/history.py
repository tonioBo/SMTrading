"""The «Andamento» chart: each portfolio's equity over a range, from the `equity_snapshots`
(the worker writes a `periodic` row every 5 minutes and a `day_open` row per local day,
so nothing is valued on read).

- `1d`, `1w`: the intraday points (every snapshot with ts in [now - span, now]);
- `1m`, `3m`, `all`: one point per day (the `day_open` rows of the range) closed by the
  portfolio's latest snapshot, so the curve reaches today; a portfolio with no `day_open`
  in the range (just created) gets every snapshot instead.
Every series is capped to `MAX_SERIES_POINTS` (`downsample`). `change_pct` is the change
from the first to the last point (None without a positive first value): the dashboard
compares portfolios of different sizes in percent.

For `1w` each portfolio carries the weekly goal while it is on (`app_settings`
«Obiettivo settimanale»): from the local Monday 00:00, base = its Monday open (or its
first snapshot of the week, see `telegram.weekly_goal.portfolio_weeks`), target = base
grown by the goal percent (default +2%). Admin-only data: never reaches the agents."""

from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.performance.service import downsample
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.telegram.weekly_goal import (
    goal_target,
    load_weekly_goal,
    portfolio_weeks,
    week_monday,
)

RANGES: tuple[str, ...] = ("1d", "1w", "1m", "3m", "all")
SPANS: dict[str, timedelta | None] = {
    "1d": timedelta(days=1), "1w": timedelta(weeks=1), "1m": timedelta(days=30),
    "3m": timedelta(days=90), "all": None,
}
INTRADAY = frozenset({"1d", "1w"})
_Q2 = Decimal("0.01")

Point = tuple[datetime, Decimal]


def _eur(value: Decimal | None) -> Decimal | None:
    return None if value is None else quantize_money(value, "EUR")


async def _portfolios(session: AsyncSession, portfolio_id: UUID | None) -> list[Portfolio]:
    if portfolio_id is not None:
        p = await session.get(Portfolio, portfolio_id)
        if p is None:
            raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found",
                           {"portfolio_id": str(portfolio_id)})
        return [p]
    return list(await session.scalars(
        select(Portfolio).where(Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)))


async def _all_rows(session: AsyncSession, ids: list[UUID], start: datetime | None,
                    now: datetime, *, day_open_only: bool = False) -> dict[UUID, list[Point]]:
    stmt = (
        select(EquitySnapshot.portfolio_id, EquitySnapshot.ts, EquitySnapshot.equity_eur)
        .where(EquitySnapshot.portfolio_id.in_(ids), EquitySnapshot.ts <= now)
        .order_by(EquitySnapshot.portfolio_id, EquitySnapshot.ts, EquitySnapshot.id)
    )
    if start is not None:
        stmt = stmt.where(EquitySnapshot.ts >= start)
    if day_open_only:
        stmt = stmt.where(EquitySnapshot.kind == SnapshotKind.day_open)
    out: dict[UUID, list[Point]] = {}
    for pid, ts, equity in (await session.execute(stmt)).all():
        out.setdefault(pid, []).append((ts, equity))
    return out


async def _latest(session: AsyncSession, ids: list[UUID], start: datetime | None,
                  now: datetime) -> dict[UUID, Point]:
    stmt = (
        select(EquitySnapshot.portfolio_id, EquitySnapshot.ts, EquitySnapshot.equity_eur)
        .ext(distinct_on(EquitySnapshot.portfolio_id))
        .where(EquitySnapshot.portfolio_id.in_(ids), EquitySnapshot.ts <= now)
        .order_by(EquitySnapshot.portfolio_id, EquitySnapshot.ts.desc(),
                  EquitySnapshot.id.desc())
    )
    if start is not None:
        stmt = stmt.where(EquitySnapshot.ts >= start)
    return {pid: (ts, eq) for pid, ts, eq in (await session.execute(stmt)).all()}


async def _series(session: AsyncSession, ids: list[UUID], range_: str,
                  start: datetime | None, now: datetime) -> dict[UUID, list[Point]]:
    if range_ in INTRADAY:
        return await _all_rows(session, ids, start, now)
    daily = await _all_rows(session, ids, start, now, day_open_only=True)
    latest = await _latest(session, ids, start, now)
    fallback = [pid for pid in ids if pid in latest and pid not in daily]
    if fallback:
        daily.update(await _all_rows(session, fallback, start, now))
    for pid, points in daily.items():
        last = latest.get(pid)
        if last is not None and last[0] > points[-1][0]:
            points.append(last)
    return daily


async def _goals(session: AsyncSession, now: datetime,
                 zone: ZoneInfo) -> tuple[Decimal | None, dict[UUID, dict[str, Any]]]:
    goal = await load_weekly_goal(session)
    if not goal.on:
        return None, {}
    week_start = datetime.combine(week_monday(now, zone), time(0), tzinfo=zone).astimezone(UTC)
    return goal.pct, {
        w.portfolio_id: {
            "week_start": week_start, "base_eur": _eur(w.start),
            "target_eur": _eur(goal_target(goal.pct, w.start)),
        }
        for w in await portfolio_weeks(session, now, zone)
    }


def _item(p: Portfolio, points: list[Point], goal: dict[str, Any] | None) -> dict[str, Any]:
    first = points[0][1] if points else None
    last = points[-1][1] if points else None
    change_pct = None
    if first is not None and first > 0:
        change_pct = ((last / first - 1) * 100).quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return {
        "id": p.id, "name": p.name,
        "series": [{"ts": ts, "equity_eur": _eur(eq)} for ts, eq in downsample(points)],
        "first_eur": _eur(first), "last_eur": _eur(last),
        "change_eur": None if first is None else _eur(last - first),
        "change_pct": change_pct,
        "goal": goal,
    }


async def equity_history(session: AsyncSession, range_: str, *, now: datetime,
                         zone: ZoneInfo, portfolio_id: UUID | None = None) -> dict[str, Any]:
    if range_ not in SPANS:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"range must be one of {list(RANGES)}",
            {"field": "range", "allowed": list(RANGES)},
        )
    span = SPANS[range_]
    start = None if span is None else now - span
    portfolios = await _portfolios(session, portfolio_id)
    ids = [p.id for p in portfolios]
    series = await _series(session, ids, range_, start, now) if ids else {}
    goal_pct, goals = (await _goals(session, now, zone)) if range_ == "1w" else (None, {})
    return {
        "range": range_, "start": start, "end": now, "goal_pct": goal_pct,
        "portfolios": [_item(p, series.get(p.id, []), goals.get(p.id)) for p in portfolios],
    }
