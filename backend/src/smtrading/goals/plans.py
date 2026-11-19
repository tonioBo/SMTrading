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
The worker job (`goals.plan_closer`) applies them. A position of an ended piano still
open (pending, grace, closing) takes no addition: no order that would increase it or turn
it to the other side (`adds_to`), refused with `plan_grace_error` by the risk check
(`risk.checks`), again at fill time (`engine.execution`) and before a send to the broker
(`brokers.sync`); the job cancels the resting ones (`goals.plan_closer`)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.models import Goal, PlanClosure
from smtrading.goals.periods import Period, goal_period, previous_period
from smtrading.instruments.market_hours import CALENDARS, local_date, session_on
from smtrading.portfolios.models import Portfolio, PortfolioStatus

GRACE_BY_HORIZON = {"week": 1, "month": 3, "quarter": 5, "year": 10}
GRACE_MIN, GRACE_MAX = 1, 10
_FX_CLOSE = time(22, 0, tzinfo=UTC)
_MAX_SCAN_DAYS = 60  # calendar days scanned looking for the Nth session
OPEN_STATES = ("pending", "grace", "closing")
PLAN_GRACE = "plan_grace"


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


def adds_to(held: Decimal, buy: bool, qty: Decimal | None) -> bool:
    """An order of `qty` (None: a notional order) on the buy (`buy`) or sell side would
    increase the position `held`, open one from flat, or turn it to the other side (long
    1, sell 3: short 2). A notional order is judged by its side alone."""
    if qty is None:
        return held == 0 or (held > 0) == buy
    after = held + qty if buy else held - qty
    return abs(after) > abs(held) or (held != 0 and after != 0 and (after > 0) != (held > 0))


def plan_grace_error(row: PlanClosure, symbol: str) -> SMTError:
    """The refusal of an addition to a position of an ended piano (RISK_LIMIT_EXCEEDED,
    details {reason: plan_grace, symbol, deadline}; deadline null before its first
    session)."""
    deadline = row.deadline.isoformat() if row.deadline else None
    until = f"until {deadline}" if deadline else "until its market's first session"
    return SMTError(
        ErrorCode.RISK_LIMIT_EXCEEDED,
        f"{symbol} is being closed at the end of a plan ({until}): no additions and no "
        "reversal to the opposite side",
        {"reason": PLAN_GRACE, "symbol": symbol, "deadline": deadline})
