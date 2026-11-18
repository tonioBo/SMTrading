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

For `1w` each portfolio carries «the weekly goal» while it is active
(`goals.service.weekly_goal`, the compatibility of the former setting): from the local
Monday 00:00, base = its Monday open (or its first snapshot of the week, see
`telegram.weekly_goal.portfolio_weeks`), target = base grown by the goal percent.

`goals` (every range): one item per active goal whose current period overlaps the range,
for the shown scope (all portfolios: the total goals and the goals of the listed
portfolios; one portfolio: its goals only): `goal_id, name, horizon, scope, portfolio_id,
kind, benchmark, target_pct, period_start, period_end` (UTC instants of the local
midnights opening and closing the period), `base_eur` (the goal's start equity,
`goals.progress`) and `target_eur` (`goals.progress.kind_target`: for max_loss the floor,
for beat_index the benchmark's change plus the margin; null without data).
`benchmarks` ({symbol: [{ts, pct}]}): for each benchmark of a listed beat_index goal, its
change in EUR from the goal's period start over the range
(`goals.benchmark.benchmark_series`, downsampled), empty without such a goal. One series
per symbol: when listed goals on the same index have different periods, the one whose
period starts latest gives it (the client rebases each series to the range start).
Admin-only data: never reaches the agents."""

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
from smtrading.goals.benchmark import benchmark_change, benchmark_series
from smtrading.goals.progress import (
    current_period,
    kind_target,
    local_midnight,
    portfolio_periods,
    visible_goals,
)
from smtrading.performance.service import downsample
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.telegram.weekly_goal import (
    goal_target,
    load_weekly_goal,
    portfolio_weeks,
    week_monday,
)

RANGES: tuple[str, ...] = ("2h", "6h", "12h", "1d", "1w", "1m", "3m", "all")
SPANS: dict[str, timedelta | None] = {
    "2h": timedelta(hours=2), "6h": timedelta(hours=6), "12h": timedelta(hours=12),
    "1d": timedelta(days=1), "1w": timedelta(weeks=1), "1m": timedelta(days=30),
    "3m": timedelta(days=90), "all": None,
}
INTRADAY = frozenset({"2h", "6h", "12h", "1d", "1w"})
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


async def _goal_lines(session: AsyncSession, portfolios: list[Portfolio], single: bool,
                      start: datetime | None, now: datetime,
                      zone: ZoneInfo) -> tuple[list[dict[str, Any]], dict[str, list]]:
    today = now.astimezone(zone).date()
    ids = {p.id for p in portfolios}
    out = []
    bench_periods: dict[str, object] = {}  # symbol -> the latest-starting period
    for goal, _ in await visible_goals(session):
        if (goal.scope == "total" and single) or (
                goal.scope == "portfolio" and goal.portfolio_id not in ids):
            continue
        period = current_period(goal, today)
        opens = local_midnight(period.start, zone)
        closes = local_midnight(period.end + timedelta(days=1), zone)
        if opens > now or (start is not None and closes < start):
            continue
        parts = await portfolio_periods(
            session, period, now, zone, None if goal.scope == "total" else [goal.portfolio_id])
        base = sum((w.start for w in parts), Decimal(0)) if parts else None
        bench = None
        if goal.kind == "beat_index" and goal.benchmark:
            bench = await benchmark_change(session, goal.benchmark, period, now, zone)
            known = bench_periods.get(goal.benchmark)
            if known is None or period.start > known.start:
                bench_periods[goal.benchmark] = period
        target = None if base is None else kind_target(goal.kind, goal.target_pct, base, bench)
        out.append({
            "goal_id": goal.id, "name": goal.name, "horizon": goal.horizon,
            "scope": goal.scope, "portfolio_id": goal.portfolio_id, "kind": goal.kind,
            "benchmark": goal.benchmark, "target_pct": goal.target_pct,
            "period_start": opens, "period_end": closes, "base_eur": _eur(base),
            "target_eur": _eur(target),
        })
    benchmarks: dict[str, list] = {}
    for symbol, period in sorted(bench_periods.items()):
        series = await benchmark_series(session, symbol, period, start, now, zone)
        benchmarks[symbol] = [{"ts": ts, "pct": pct.quantize(_Q2, rounding=ROUND_HALF_EVEN)}
                              for ts, pct in downsample(series)]
    return out, benchmarks


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
    lines, benchmarks = await _goal_lines(session, portfolios, portfolio_id is not None,
                                          start, now, zone)
    return {
        "range": range_, "start": start, "end": now, "goal_pct": goal_pct,
        "portfolios": [_item(p, series.get(p.id, []), goals.get(p.id)) for p in portfolios],
        "goals": lines, "benchmarks": benchmarks,
    }
