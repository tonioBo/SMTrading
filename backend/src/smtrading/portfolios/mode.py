"""The portfolio "modalità" on the path simulazione -> paper -> reale, derived from the
broker link alone (no stored column): no link = simulazione (virtual money, real market
prices); a link = paper (every adapter today is a paper/demo account); "reale" is
reserved for plan 8 and never produced yet."""

import enum

from smtrading.portfolios.models import Portfolio


class PortfolioMode(enum.StrEnum):
    simulazione = "simulazione"
    paper = "paper"
    reale = "reale"


def portfolio_mode(portfolio: Portfolio) -> PortfolioMode:
    if portfolio.broker_link_id is None:
        return PortfolioMode.simulazione
    return PortfolioMode.paper
