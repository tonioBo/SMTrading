"""The portfolio "modalità" on the path simulazione -> paper -> reale, derived from the
broker link alone (no stored column): no link = simulazione (virtual money, real market
prices); a paper/demo link = paper; a real-money link (LIVE_KINDS) = reale only while it
is `armed` (real orders are sent). A real-money link that is `configured` or in
`dry_run` sends nothing: its portfolio is reported as paper, and `live_state_of` tells
the three live states apart for the screens that show them."""

import enum

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.portfolios.models import Portfolio

ARMED = "armed"  # brokers.live.models.LiveState.armed (not imported: no model cycle)


class PortfolioMode(enum.StrEnum):
    simulazione = "simulazione"
    paper = "paper"
    reale = "reale"


def portfolio_mode(portfolio: Portfolio, link_kind: str | None = None,
                   live_state: str | None = None) -> PortfolioMode:
    """`link_kind`/`live_state`: those of the portfolio's broker link (callers that
    have the link pass them; without them a linked portfolio is paper)."""
    if portfolio.broker_link_id is None:
        return PortfolioMode.simulazione
    if link_kind in LIVE_KINDS and live_state == ARMED:
        return PortfolioMode.reale
    return PortfolioMode.paper


def live_state_of(link: BrokerLink | None) -> str | None:
    """The live state of a real-money link (configured | dry_run | armed), None for no
    link or a paper/demo link."""
    if link is None or link.kind not in LIVE_KINDS:
        return None
    return link.live_state


async def load_portfolio_mode(session: AsyncSession, portfolio: Portfolio) -> PortfolioMode:
    """portfolio_mode with the link read from the database (identity map first)."""
    if portfolio.broker_link_id is None:
        return PortfolioMode.simulazione
    link = await session.get(BrokerLink, portfolio.broker_link_id)
    if link is None:
        return PortfolioMode.paper
    return portfolio_mode(portfolio, link.kind, link.live_state)
