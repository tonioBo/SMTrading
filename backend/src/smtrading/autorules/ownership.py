"""One owner per (portfolio, symbol) across both modes and manual orders: who owns a pair
(`owner_rule`, read by risk step 11) and which pairs mode A must not evaluate
(`owned_symbols`)."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.tables import ACTIVE, OPEN, SENT, AutoRule, AutoTrade
from smtrading.portfolios.models import Portfolio


async def owner_rule(session: AsyncSession, portfolio_id: uuid.UUID,
                     symbol: str) -> uuid.UUID | None:
    """The `active` rule on (portfolio, symbol); else the rule of an auto trade still `sent`
    or `open` on the pair (a paused rule keeps its pair until its trade is closed)."""
    active = await session.scalar(select(AutoRule.id).where(
        AutoRule.portfolio_id == portfolio_id, AutoRule.symbol == symbol,
        AutoRule.status == ACTIVE))
    if active is not None:
        return active
    return await session.scalar(
        select(AutoTrade.rule_id).where(
            AutoTrade.portfolio_id == portfolio_id, AutoTrade.symbol == symbol,
            AutoTrade.status.in_((SENT, OPEN)))
        .order_by(AutoTrade.id).limit(1))


async def owned_symbols(session: AsyncSession) -> frozenset[tuple[uuid.UUID, str]]:
    """The (portfolio, symbol) pairs of the active rules of portfolios with both
    `intraday_enabled` and `auto_rules_enabled`: one query; the `exclude_provider` of the
    intraday job."""
    rows = await session.execute(
        select(AutoRule.portfolio_id, AutoRule.symbol)
        .join(Portfolio, Portfolio.id == AutoRule.portfolio_id)
        .where(AutoRule.status == ACTIVE, Portfolio.intraday_enabled.is_(True),
               Portfolio.auto_rules_enabled.is_(True)))
    return frozenset((pid, sym) for pid, sym in rows.all())
