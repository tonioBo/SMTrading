"""The portfolio row lock (Ruling R11): taken FOR UPDATE before any cash/position write,
always before the chain lock (which post_journal takes)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
