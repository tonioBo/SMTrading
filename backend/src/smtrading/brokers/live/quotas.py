"""Cash quotas of the portfolios sharing one real-money account (plan 9, Task 11).

A real-money link may hold several portfolios. Each one's **quota** is its own cash
(every currency) in the account currency, counted at the worst rate for the account: for
a currency `c` other than the account's, the higher of the current rate and the link's
reference rate (`BrokerLink.fx_ref_rates[c]`, units of the account currency per unit of
`c`), plus FX_SAFETY_PCT. The quota of a portfolio holds the cost of its open buys until
they fill, so `broker cash − Σ other quotas` is what a portfolio may spend at the broker
and nothing is counted twice. The cash not covered by any quota (`unassigned`) stays idle.

Linking, the cash cap of a real buy (risk.live_checks), the deposit guard and the admin
link card use these functions. No network I/O here: the FX rates come from the caller's
QuoteSource (BrokerSync's frozen memo, or the request's MemoQuoteSource outside locks).
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import QuoteSource
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances
from smtrading.risk.live_checks import FX_SAFETY_PCT, fx_ref_rate

NO_FX_MESSAGE = "cambio non disponibile per la quota del portafoglio"
_PREC = 60


@dataclass(frozen=True)
class Quota:
    portfolio_id: UUID
    name: str
    quota: Decimal  # in the account currency


def account_currency(link: BrokerLink) -> str | None:
    """The account currency of the link's last snapshot, upper case; None when unknown."""
    snap = link.last_account_snapshot if isinstance(link.last_account_snapshot, dict) else {}
    ccy = snap.get("currency")
    return str(ccy).upper() if ccy else None


async def to_account(qs: QuoteSource | None, link: BrokerLink, amount: Decimal, ccy: str,
                     account: str) -> Decimal:
    """`amount` of `ccy` in the account currency at the worst rate for the account (module
    docstring). PROVIDER_UNAVAILABLE without a current rate."""
    if amount == 0 or ccy.upper() == account.upper():
        return amount
    if qs is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, NO_FX_MESSAGE,
                       {"base": ccy, "quote": account})
    rate = await qs.get_fx_rate(ccy, account)
    ref = fx_ref_rate(link, ccy)
    with localcontext() as ctx:
        ctx.prec = _PREC
        worst = rate if ref is None else max(rate, ref)
        return amount * worst * (1 + FX_SAFETY_PCT)


async def quota_of_balances(qs: QuoteSource | None, link: BrokerLink,
                            balances: dict[str, Decimal], account: str) -> Decimal:
    total = Decimal(0)
    for ccy, balance in sorted(balances.items()):
        if balance:
            total += await to_account(qs, link, balance, ccy, account)
    return total


async def portfolio_quota(session: AsyncSession, qs: QuoteSource | None,
                          portfolio: Portfolio, link: BrokerLink, *,
                          currency: str | None = None) -> Decimal:
    """The portfolio's quota in the account currency (`currency`, default the link's last
    snapshot currency). PROVIDER_UNAVAILABLE without an FX rate (or account currency)."""
    account = (currency or account_currency(link) or "").upper()
    balances = await get_cash_balances(session, portfolio.id)
    if not account:
        if not any(balances.values()):
            return Decimal(0)
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, NO_FX_MESSAGE, {"quote": None})
    return await quota_of_balances(qs, link, balances, account)


async def linked_portfolios(session: AsyncSession, link_id: UUID) -> list[Portfolio]:
    return list(await session.scalars(
        select(Portfolio).where(Portfolio.broker_link_id == link_id)
        .order_by(Portfolio.name, Portfolio.id)))


async def link_quotas(session: AsyncSession, qs: QuoteSource | None, link: BrokerLink, *,
                      include: Portfolio | None = None, exclude: UUID | None = None,
                      currency: str | None = None) -> list[Quota]:
    """The quotas of the link's portfolios, plus `include` when not linked yet, less
    `exclude`; ordered by name."""
    portfolios = await linked_portfolios(session, link.id)
    if include is not None and all(p.id != include.id for p in portfolios):
        portfolios.append(include)
    out = [Quota(p.id, p.name, await portfolio_quota(session, qs, p, link, currency=currency))
           for p in portfolios if p.id != exclude]
    return sorted(out, key=lambda q: (q.name, str(q.portfolio_id)))


def quotas_total(quotas: list[Quota]) -> Decimal:
    return sum((q.quota for q in quotas), Decimal(0))


def unassigned(broker_cash: Decimal, quotas: list[Quota]) -> Decimal:
    """The broker cash no quota covers (negative when the quotas exceed it)."""
    return broker_cash - quotas_total(quotas)
