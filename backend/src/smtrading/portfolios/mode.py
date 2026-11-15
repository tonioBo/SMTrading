"""The portfolio "modalità" on the path simulazione -> paper -> reale, derived from the
broker link alone (no stored column): no link = simulazione (virtual money, real market
prices); a paper/demo link = paper; a real-money link (LIVE_KINDS) = reale only while it
is `armed` (real orders are sent). A real-money link that is `configured` or in
`dry_run` sends nothing: its portfolio is reported as paper, and `live_state_of` tells
the three live states apart for the screens that show them."""

import enum

from sqlalchemy import select
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


async def portfolio_links(session: AsyncSession, portfolios) -> dict:
    """The broker link of each linked portfolio ({link_id: BrokerLink}), one query."""
    ids = {p.broker_link_id for p in portfolios if p.broker_link_id is not None}
    if not ids:
        return {}
    return {link.id: link for link in await session.scalars(
        select(BrokerLink).where(BrokerLink.id.in_(ids)))}


def mode_fields(portfolio: Portfolio, link: BrokerLink | None) -> dict:
    """`{"mode", "live_state"}` of a portfolio given its link (the DTOs' fields)."""
    return {
        "mode": portfolio_mode(portfolio, link.kind if link else None,
                               link.live_state if link else None),
        "live_state": live_state_of(link),
    }


# The portal's names for the mode (admin DTOs only: agents never see these, see
# mcp/tools_portfolio.py, which keeps mode_fields).
BROKER_NAMES = {"alpaca": "Alpaca", "t212": "Trading 212", "etoro": "eToro"}  # kind prefix
PRACTICE_WORD = {"alpaca_paper": "prova", "t212_demo": "demo", "etoro_demo": "demo"}
SIMULATED_LABEL = "SMTrading (simulato)"
_LIVE_SUFFIX = {"dry_run": " · prova a secco", "configured": " · sospeso"}


def mode_label(portfolio: Portfolio, link: BrokerLink | None) -> str:
    """What the portal shows for the portfolio's mode, with the broker's name:
    «SMTrading (simulato)», «Alpaca · prova», «🔴 Trading 212 · reale · sospeso»…
    An unknown kind is shown as it is."""
    if link is None:
        return SIMULATED_LABEL
    name = BROKER_NAMES.get(link.kind.split("_", 1)[0])
    if name is None:
        return link.kind
    if link.kind in PRACTICE_WORD:
        return f"{name} · {PRACTICE_WORD[link.kind]}"
    if link.kind in LIVE_KINDS:
        return f"🔴 {name} · reale{_LIVE_SUFFIX.get(link.live_state, '')}"
    return link.kind


def admin_mode_fields(portfolio: Portfolio, link: BrokerLink | None) -> dict:
    """mode_fields plus the portal-only `broker_kind` and `mode_label`."""
    return {
        **mode_fields(portfolio, link),
        "broker_kind": link.kind if link else None,
        "mode_label": mode_label(portfolio, link),
    }


def mode_counts(rows) -> list[dict]:
    """`[{label, count, live}]` of (portfolio, link) pairs grouped by mode_label: the
    simulated ones first, then practice/demo alphabetically, then real-money ones."""
    counts: dict[str, list] = {}
    for portfolio, link in rows:
        label = mode_label(portfolio, link)
        entry = counts.setdefault(label, [0, link is None, live_state_of(link) is not None])
        entry[0] += 1
    order = sorted(counts.items(), key=lambda kv: (not kv[1][1], kv[1][2], kv[0]))
    return [{"label": label, "count": n, "live": live} for label, (n, _, live) in order]
