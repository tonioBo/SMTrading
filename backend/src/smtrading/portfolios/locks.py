"""Row locks and their order.

Ruling R11: the portfolio row is taken FOR UPDATE before any cash/position write, always
before the chain lock (which post_journal takes).

Ruling R8.9 (real money), one lock order everywhere: the broker link row first (FOR NO
KEY UPDATE: inserting or updating rows that reference it, an order, a portfolio, a live
audit, only takes FOR KEY SHARE on it and is never blocked), then the portfolio row(s)
in id order, then their orders. A transaction holding a portfolio lock never locks or
writes the link row afterwards (BrokerSync marks the snapshot stale in its own short
transaction). Users: the activation service (save keys, test, arm, disarm, link), the
live reconciliation and monitor, and every `trip_reduce_only` caller that holds a link
lock (it takes the portfolio lock after it).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio, ensure_not_archived


async def lock_portfolio(session: AsyncSession, portfolio_id: UUID) -> Portfolio:
    """The portfolio row, locked FOR UPDATE and refreshed; NOT_FOUND if missing."""
    portfolio = (
        await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if portfolio is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    return portfolio


async def lock_live_portfolio(session: AsyncSession, portfolio_id: UUID) -> Portfolio:
    """lock_portfolio, then PORTFOLIO_ARCHIVED if the portfolio is archived."""
    return ensure_not_archived(await lock_portfolio(session, portfolio_id))


async def lock_broker_link(session: AsyncSession, link_id: UUID) -> BrokerLink | None:
    """The broker link row, locked FOR NO KEY UPDATE and refreshed (None if missing).
    Taken before any portfolio lock (R8.9)."""
    return (
        await session.execute(
            select(BrokerLink).where(BrokerLink.id == link_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
