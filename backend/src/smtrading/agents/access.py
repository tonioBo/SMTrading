"""Which portfolios an agent can reach at all: a portfolio with no `agent_portfolios` row
is the owner's alone (e.g. an owner's test portfolio). What concerns it must never reach
the agents (no Paperclip issue, no activity row): the owner is told on Telegram instead."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.models import AgentPortfolio


async def portfolios_with_agents(session: AsyncSession,
                                 portfolio_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """The ones of `portfolio_ids` that at least one agent can access."""
    ids = set(portfolio_ids)
    if not ids:
        return set()
    return set(await session.scalars(
        select(AgentPortfolio.portfolio_id).where(AgentPortfolio.portfolio_id.in_(ids))
        .distinct()))
