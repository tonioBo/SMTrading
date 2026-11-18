"""The «piani»: goals with `closes_positions` (pure rules, plan 17).

A piano closes the positions open at the end of its period (`goals.periods`):
- the end is local midnight after the period's last day (`ended_at`);
- the grace, in market days, by horizon: week 1, month 3, quarter 5, year 10; custom 10%
  of its calendar days, rounded half up, from 1 to 10 (`grace_days`);
- the market days are the sessions of the position's exchange (holidays and manual
  closures applied) closing after the end; CRYPTO counts calendar days; FX Monday to
  Friday, each closing at 22:00 UTC; outside the calendars' window Monday to Friday too;
- the grace deadline is the close of the Nth session (CRYPTO: the end + N days)
  (`grace_deadline`).
The worker job (`goals.plan_closer`) applies them; the risk check refuses to increase a
position of an ended piano still open (`risk.checks`)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.goals.models import Goal, PlanClosure
from smtrading.goals.periods import Period, goal_period, previous_period
from smtrading.instruments.market_hours import CALENDARS, local_date, session_on
from smtrading.portfolios.models import Portfolio, PortfolioStatus

GRACE_BY_HORIZON = {"week": 1, "month": 3, "quarter": 5, "year": 10}
GRACE_MIN, GRACE_MAX = 1, 10
_FX_CLOSE = time(22, 0, tzinfo=UTC)
_MAX_SCAN_DAYS = 60  # calendar days scanned looking for the Nth session
OPEN_STATES = ("pending", "grace", "closing")


def grace_days(horizon: str, period: Period) -> int:
    """The market days of grace of a piano of `horizon` over `period`."""
    if horizon in GRACE_BY_HORIZON:
        return GRACE_BY_HORIZON[horizon]
    days = Decimal((period.end - period.start).days + 1)
    n = int((days / 10).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(GRACE_MAX, max(GRACE_MIN, n))


def ended_at(period: Period, zone: ZoneInfo) -> datetime:
    """The end of `period`: local midnight after its last day, in UTC."""
    return datetime.combine(period.end + timedelta(days=1), time(0), tzinfo=zone
                            ).astimezone(UTC)


def last_ended_period(horizon: str, start_date: date, end_date: date | None,
                      recurring: bool, today: date) -> Period | None:
    """The latest period of a goal over before `today` (None while none is)."""
    current = goal_period(horizon, start_date, end_date, recurring, today)
    if current.end < today:
        return current
    if not recurring:
        return None
    previous = previous_period(horizon, current)
    return previous if previous.end >= start_date else None


def _weekday_deadline(after: datetime, n: int) -> datetime:
    """The 22:00 UTC of the Nth weekday closing after `after`."""
    day, count = after.astimezone(UTC).date(), 0
    for _ in range(_MAX_SCAN_DAYS):
        close = datetime.combine(day, _FX_CLOSE)
        if day.weekday() < 5 and close > after:
            count += 1
            if count == n:
                return close
        day += timedelta(days=1)
    raise AssertionError("unreachable: a weekday comes within 60 days")


def grace_deadline(market: str, after: datetime, n: int) -> datetime:
    """The close of the `n`th session of `market` closing after `after` (see the module
    docstring)."""
    if market == "CRYPTO":
        return after + timedelta(days=n)
    if market not in CALENDARS:
        return _weekday_deadline(after, n)
    day, count = local_date(market, after), 0
    try:
        for _ in range(_MAX_SCAN_DAYS):
            session = session_on(market, day)
            if session is not None and session[1] > after:
                count += 1
                if count == n:
                    return session[1]
            day += timedelta(days=1)
    except ValueError:  # outside the calendar's window
        return _weekday_deadline(after, n)
    return _weekday_deadline(after, n)


# -- the open closings (risk check, MCP, admin API, the job) ----------------------------


def plan_rows():
    """The open rows of active piani in portfolios not archived, of periods ended after
    the goal last became a piano (`plan_since`): a piano turned off and on again leaves
    the rows of its earlier periods alone (never closed, no longer blocking additions)."""
    return (select(PlanClosure)
            .join(Goal, Goal.id == PlanClosure.goal_id)
            .join(Portfolio, Portfolio.id == PlanClosure.portfolio_id)
            .where(PlanClosure.state.in_(OPEN_STATES), Goal.active.is_(True),
                   Goal.closes_positions.is_(True), Goal.plan_since.is_not(None),
                   PlanClosure.ended_at > Goal.plan_since,
                   Portfolio.status != PortfolioStatus.archived))


async def open_closure(session: AsyncSession, portfolio_id: UUID,
                       symbol: str) -> PlanClosure | None:
    """The open closing of the position (`portfolio_id`, `symbol`), if any."""
    return await session.scalar(
        plan_rows().where(PlanClosure.portfolio_id == portfolio_id,
                           PlanClosure.symbol == symbol)
        .order_by(PlanClosure.id).limit(1))


async def open_closures(session: AsyncSession,
                        portfolio_ids: list[UUID] | None = None) -> list[PlanClosure]:
    stmt = plan_rows().order_by(PlanClosure.id)
    if portfolio_ids is not None:
        stmt = stmt.where(PlanClosure.portfolio_id.in_(portfolio_ids))
    return list(await session.scalars(stmt))


def grace_until(row: PlanClosure | None) -> datetime | None:
    """The `plan_grace_until` of a position: its grace deadline while in grace."""
    return row.deadline if row is not None and row.state == "grace" else None
