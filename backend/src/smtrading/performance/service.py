"""Portfolio performance from the `equity_snapshots` (periodic and day_open rows).

- `series`: snapshots with ts >= now - period, oldest first, equity in cents, downsampled
  for transport: periods of 1m and longer keep only the `day_open` points (all points when
  the period has no day_open yet); every series is then cut into at most `MAX_SERIES_POINTS`
  equal buckets of consecutive points, keeping the last point of each bucket;
- `return_pct` = (last / first - 1) x 100;
- `max_drawdown_pct`: the deepest fall from a running peak, (value / peak - 1) x 100,
  so 0 or negative;
- `sharpe`: mean / sample standard deviation of the day-over-day returns of the
  `day_open` snapshots in the period, x sqrt(252); None with fewer than 3 day_open
  points or a zero deviation.

The statistics are computed on every snapshot of the period (periodic and day_open), not
on the downsampled series. Ratios are Decimal quantized to 4 decimals (None when there is no series). External
cash flows are not neutralised: a deposit shows up as a return.
"""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from itertools import pairwise
from typing import Literal, get_args
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.risk.models import EquitySnapshot, SnapshotKind

Period = Literal["1d", "1w", "1m", "3m", "1y", "all"]
PERIODS: tuple[str, ...] = get_args(Period)
SPANS = {
    "1d": timedelta(days=1), "1w": timedelta(weeks=1), "1m": timedelta(days=30),
    "3m": timedelta(days=90), "1y": timedelta(days=365), "all": None,
}
_DAILY_SERIES_PERIODS = frozenset({"1m", "3m", "1y", "all"})
MAX_SERIES_POINTS = 500
_TRADING_DAYS = 252
_PREC = 60
_Q4 = Decimal("0.0001")


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_Q4, rounding=ROUND_HALF_EVEN)


def _return_pct(first: Decimal, last: Decimal) -> Decimal | None:
    if first <= 0:
        return None
    return _q4((last / first - 1) * 100)


def _max_drawdown_pct(values: list[Decimal]) -> Decimal:
    peak: Decimal | None = None
    worst = Decimal(0)
    for v in values:
        if peak is None or v > peak:
            peak = v
        if peak > 0:
            worst = min(worst, (v / peak - 1) * 100)
    return _q4(worst)


def _sharpe(day_opens: list[Decimal]) -> Decimal | None:
    if len(day_opens) < 3 or any(v <= 0 for v in day_opens[:-1]):
        return None
    returns = [b / a - 1 for a, b in pairwise(day_opens)]
    n = len(returns)
    mean = sum(returns, Decimal(0)) / n
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (n - 1)
    if variance == 0:
        return None
    return _q4(mean / variance.sqrt() * Decimal(_TRADING_DAYS).sqrt())


def downsample[T](points: list[T], limit: int = MAX_SERIES_POINTS) -> list[T]:
    """At most `limit` points: the last of each run of ceil(n / limit) consecutive points."""
    size = -(-len(points) // limit)
    if size <= 1:
        return points
    return [points[min(i + size, len(points)) - 1] for i in range(0, len(points), size)]


async def get_performance(
    session: AsyncSession, portfolio_id: UUID, period: Period, *,
    now: datetime | None = None,
) -> dict:
    if period not in SPANS:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"period must be one of {list(PERIODS)}",
            {"field": "period", "allowed": list(PERIODS)},
        )
    span = SPANS[period]
    stmt = (
        select(EquitySnapshot.ts, EquitySnapshot.kind, EquitySnapshot.equity_eur)
        .where(EquitySnapshot.portfolio_id == portfolio_id)
        .order_by(EquitySnapshot.ts, EquitySnapshot.id)
    )
    if span is not None:
        stmt = stmt.where(EquitySnapshot.ts >= (now or datetime.now(UTC)) - span)
    rows = (await session.execute(stmt)).all()

    if not rows:
        return {"series": [], "return_pct": None, "max_drawdown_pct": None, "sharpe": None}
    points = rows
    if period in _DAILY_SERIES_PERIODS:
        points = [r for r in rows if r[1] == SnapshotKind.day_open] or rows
    series = [
        {"ts": ts, "equity_eur": quantize_money(eq, "EUR")} for ts, _, eq in downsample(points)
    ]
    values = [eq for _, _, eq in rows]
    with localcontext() as ctx:
        ctx.prec = _PREC
        return {
            "series": series,
            "return_pct": _return_pct(values[0], values[-1]),
            "max_drawdown_pct": _max_drawdown_pct(values),
            "sharpe": _sharpe([eq for _, kind, eq in rows if kind == SnapshotKind.day_open]),
        }
