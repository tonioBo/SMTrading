"""The deposit guard of a real-money portfolio (plan 9, Task 7, user decision 2026-11-15).

A deposit on a portfolio linked to a live link (`live_state` not null) is registered only
when the broker account already holds that cash: the quotas of every portfolio of the
link (brokers.live.quotas, Task 11: each one's cash in the account currency at the worst
rate for the account, divided by 1 − FX_SAFETY_PCT) plus the deposit, converted the same
way, must not exceed the broker's available cash, read fresh from the broker (GET only,
no transaction open, R9). A reset with new cash on such a portfolio is checked the same
way (its own quota is replaced, not added to). Without an FX rate the deposit is refused
too. Withdrawals are never checked; paper portfolios are untouched.

Deposits and resets on the portfolios of one account are serialized by the link lock
(admin.routes_portfolios takes it before the portfolio lock, R8.9), so two deposits on
two portfolios cannot both pass on the same broker cash.

Every FX rate the check needs is prefetched with the account read, before any lock, into
a frozen memo: a rate missing under the lock refuses the deposit (cambio non
disponibile), it never reaches the provider.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.live.account import account_fingerprint, decrypt_live_config
from smtrading.brokers.live.quotas import (
    link_quotas,
    portfolio_quota,
    quota_currencies,
    quotas_total,
    to_account,
)
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.portfolios.models import Portfolio

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
    qs: QuoteSource  # the frozen memo with every rate the check needs


def _refused(field: str, no_fx: bool = False) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER,
                    DEPOSIT_SHORT_MESSAGE + (NO_FX_SUFFIX if no_fx else ""),
                    {"field": field})


def _unreadable() -> SMTError:
    return SMTError(ErrorCode.PROVIDER_UNAVAILABLE, UNREADABLE_MESSAGE)


async def read_live_cash(sessionmaker: async_sessionmaker, portfolio_id: uuid.UUID,
                         http_factory: Callable, qs: QuoteSource, *,
                         currency: str | None = None) -> BrokerCash | None:
    """The broker's available cash for a portfolio linked to a live link, read now
    through the live gate (GET only, no transaction open), with the FX rates of the
    check prefetched (`currency`: the deposit's, default the base currency); None for
    any other portfolio. PROVIDER_UNAVAILABLE when the keys or the account cannot be
    read, or another account than the tested one is behind the keys."""
    async with sessionmaker() as session:
        row = (await session.execute(
            select(BrokerLink, Portfolio.base_currency)
            .join(Portfolio, Portfolio.broker_link_id == BrokerLink.id)
            .where(Portfolio.id == portfolio_id, BrokerLink.live_state.is_not(None))
        )).first()
        if row is None:
            await session.commit()
            return None
        link, base = row
        link_id, kind, fingerprint = link.id, link.kind, link.account_fingerprint
        ccys = await quota_currencies(session, link.id)  # every portfolio of the link
        try:
            config = decrypt_live_config(link)  # keys registered for log masking
        except Exception:  # noqa: BLE001  (never the details: they could hold key material)
            log.error("live link %s: keys cannot be decrypted for a deposit", link_id)
            raise _unreadable() from None
        finally:
            await session.commit()
    http = http_factory(link_id)
    try:
        snap = await adapter_class(kind)(config, http).get_account()
    except Exception as e:  # noqa: BLE001  (logged masked; the deposit is not registered)
        log.warning("live link %s: account read for a deposit failed: %s", link_id, e)
        raise _unreadable() from None
    finally:
        await http.aclose()
    if fingerprint is not None and account_fingerprint(kind, snap.account_id) != fingerprint:
        log.error("live link %s: another account behind the keys; deposit refused", link_id)
        raise _unreadable()
    account_ccy = str(snap.currency).upper()
    memo = MemoQuoteSource(qs)  # the quotas' and the deposit's rates to the account's
    await memo.prefetch([], {(c, account_ccy) for c in
                             {*ccys, base.upper(), (currency or base).upper()}
                             if c != account_ccy})
    memo.freeze()
    return BrokerCash(link_id, snap.cash_available, account_ccy, memo)


async def check_live_deposit(session: AsyncSession, portfolio: Portfolio,
                             broker: BrokerCash | None, amount: Decimal, currency: str, *,
                             replace: bool = False, field: str = "amount") -> None:
    """Raises INVALID_ORDER (`field`) when the link's quotas (the portfolio's own none with
    `replace`, a reset) plus the deposit exceed the broker's cash (module docstring). The
    caller holds the link lock, then the portfolio lock, and read `broker` before them; a portfolio linked meanwhile to another
    live link is refused (PROVIDER_UNAVAILABLE, try again)."""
    link = (await session.get(BrokerLink, portfolio.broker_link_id)
            if portfolio.broker_link_id is not None else None)
    if link is None or link.live_state is None:
        return
    if broker is None or broker.link_id != link.id:
        raise _unreadable()
    qs, ccy = broker.qs, broker.currency
    try:
        others = quotas_total(await link_quotas(session, qs, link, exclude=portfolio.id,
                                                currency=ccy))
        ours = Decimal(0) if replace else await portfolio_quota(session, qs, portfolio, link,
                                                                currency=ccy)
        deposit = await to_account(qs, link, amount, currency, ccy)
    except SMTError as e:
        if e.code == ErrorCode.PROVIDER_UNAVAILABLE:
            raise _refused(field, no_fx=True) from None
        raise
    if others + ours + deposit > broker.cash:
        raise _refused(field)
