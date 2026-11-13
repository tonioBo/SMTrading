"""The effective risk profile of a portfolio, shared by pre-trade checks and execution."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import RiskProfile

DEFAULT_RISK_PROFILE_NAME = "Default"


async def effective_risk_profile(
    session: AsyncSession, portfolio: Portfolio
) -> RiskProfile | None:
    """The portfolio's profile, else the seeded Default; None means no limits."""
    if portfolio.risk_profile_id is not None:
        return await session.get(RiskProfile, portfolio.risk_profile_id)
    return await session.scalar(
        select(RiskProfile).where(RiskProfile.name == DEFAULT_RISK_PROFILE_NAME)
    )
