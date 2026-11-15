"""The deposit guard of a real-money portfolio (plan 9, Task 7, user decision 2026-11-15).

A deposit on a portfolio linked to a live link (`live_state` not null) is registered only
when the broker account already holds that cash: the portfolio's cash plus the deposit
(in its base currency) must not exceed the broker's available cash, read fresh from the
broker (GET only, no transaction open, R9) and counted as the cash cap counts it
(`broker_cash_in_base`: the worse of the current and the reference rate, less
FX_SAFETY_PCT, for an account in another currency). Without an FX rate the deposit is
refused too. Withdrawals are never checked; paper portfolios are untouched.

With several portfolios on one account the sum of the link's quotas counts (Task 11).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.live.account import account_fingerprint, decrypt_live_config
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import QuoteSource
from smtrading.portfolios.models import Portfolio
from smtrading.risk.live_checks import broker_cash_in_base, portfolio_cash, to_currency

log = logging.getLogger(__name__)

DEPOSIT_SHORT_MESSAGE = ("Il conto del broker non ha ancora questo contante: fai prima il"
                         " bonifico al broker, poi registra il deposito")
NO_FX_SUFFIX = " (cambio non disponibile)"
UNREADABLE_MESSAGE = "conto del broker non leggibile: riprova tra poco"


@dataclass(frozen=True)
class BrokerCash:
    link_id: uuid.UUID
    cash: Decimal  # available cash, in `currency`
    currency: str


def _refused(no_fx: bool = False) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER,
                    DEPOSIT_SHORT_MESSAGE + (NO_FX_SUFFIX if no_fx else ""),
                    {"field": "amount"})


async def read_live_cash(sessionmaker: async_sessionmaker, portfolio_id: uuid.UUID,
                         http_factory: Callable) -> BrokerCash | None:
    """The broker's available cash for a portfolio linked to a live link, read now
    through the live gate (GET only, no transaction open); None for any other portfolio.
    PROVIDER_UNAVAILABLE when the account cannot be read or is another account than the
    tested one."""
    async with sessionmaker() as session:
        link = await session.scalar(
            select(BrokerLink).join(Portfolio, Portfolio.broker_link_id == BrokerLink.id)
            .where(Portfolio.id == portfolio_id, BrokerLink.live_state.is_not(None)))
        if link is None:
            await session.commit()
            return None
        link_id, kind, fingerprint = link.id, link.kind, link.account_fingerprint
        config = decrypt_live_config(link)  # keys registered for log masking
        await session.commit()
    http = http_factory(link_id)
    try:
        snap = await adapter_class(kind)(config, http).get_account()
    except Exception as e:  # noqa: BLE001  (logged masked; the deposit is not registered)
        log.warning("live link %s: account read for a deposit failed: %s", link_id, e)
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, UNREADABLE_MESSAGE) from None
    finally:
        await http.aclose()
    if fingerprint is not None and account_fingerprint(kind, snap.account_id) != fingerprint:
        log.error("live link %s: another account behind the keys; deposit refused", link_id)
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, UNREADABLE_MESSAGE)
    return BrokerCash(link_id, snap.cash_available, str(snap.currency).upper())


async def check_live_deposit(session: AsyncSession, qs: QuoteSource, portfolio: Portfolio,
                             broker: BrokerCash | None, amount: Decimal,
                             currency: str) -> None:
    """Raises INVALID_ORDER (`field` amount) when the portfolio's cash plus the deposit
    exceeds the broker's (module docstring). The caller holds the portfolio lock and read
    `broker` before it; a portfolio linked meanwhile to another live link is refused
    (PROVIDER_UNAVAILABLE, try again)."""
    link = (await session.get(BrokerLink, portfolio.broker_link_id)
            if portfolio.broker_link_id is not None else None)
    if link is None or link.live_state is None:
        return
    if broker is None or broker.link_id != link.id:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, UNREADABLE_MESSAGE)
    base = portfolio.base_currency
    try:
        ours = await portfolio_cash(session, qs, portfolio)
        deposit = await to_currency(qs, amount, currency, base)
        available = await broker_cash_in_base(qs, link, broker.cash, broker.currency, base)
    except SMTError as e:
        if e.code == ErrorCode.PROVIDER_UNAVAILABLE:
            raise _refused(no_fx=True) from None
        raise
    if ours + deposit > available:
        raise _refused()
