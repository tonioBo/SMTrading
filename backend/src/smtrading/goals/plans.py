"""The «piani»: goals with `closes_positions` (pure rules, plan 17; no grace since
20/11/2026).

A piano closes every position of its portfolios open at the end of its period
(`goals.periods`), in profit or in loss: the agents close them in the pre-close cycles of
the period's last session; the end is local midnight after the period's last day
(`ended_at`), and SMTrading closes the ones still open at their market's first session
after it (`goals.plan_closer`). A position of an ended piano still open (pending,
closing; `grace` only on rows written before 20/11/2026) takes no addition: no order that
would increase it or turn it to the other side (`adds_to`), refused with
`plan_closing_error` by the risk check (`risk.checks`), again at fill time
(`engine.execution`) and before a send to the broker (`brokers.sync`); the job cancels the
resting ones (`goals.plan_closer`)."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.goals.models import Goal, PlanClosure
from smtrading.goals.periods import Period, goal_period, previous_period
from smtrading.portfolios.models import Portfolio, PortfolioStatus

# `grace`: a legacy state (rows written before 20/11/2026), handled as `pending`
OPEN_STATES = ("pending", "grace", "closing")
PLAN_CLOSING_REASON = "plan_grace"  # the refusal's details.reason, kept for compatibility


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


def adds_to(held: Decimal, buy: bool, qty: Decimal | None) -> bool:
    """An order of `qty` (None: a notional order) on the buy (`buy`) or sell side would
    increase the position `held`, open one from flat, or turn it to the other side (long
    1, sell 3: short 2). A notional order is judged by its side alone."""
    if qty is None:
        return held == 0 or (held > 0) == buy
    after = held + qty if buy else held - qty
    return abs(after) > abs(held) or (held != 0 and after != 0 and (after > 0) != (held > 0))


def plan_closing_error(symbol: str) -> SMTError:
    """The refusal of an addition to a position of an ended piano (RISK_LIMIT_EXCEEDED,
    details {reason: plan_grace, symbol})."""
    return SMTError(
        ErrorCode.RISK_LIMIT_EXCEEDED,
        f"{symbol}: posizione in chiusura per fine piano, nessuna aggiunta né inversione "
        "di lato",
        {"reason": PLAN_CLOSING_REASON, "symbol": symbol})
