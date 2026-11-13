"""Daily interest: margin loans (rate by currency, "*" fallback) and short borrow fees
(rate by asset class), `|balance| x annual_pct/100/360` per day.

Each portfolio is charged in its own transaction, claimed in job_runs as
`interest / {day}:{portfolio_id}` so a crash mid-run never charges a portfolio twice;
the whole day is marked `interest / {day}` once every portfolio succeeded.
"""

import logging
from datetime import date
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.money import quantize_money
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_balance, get_or_create_account, post_journal
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import PortfolioStatus, live_portfolio_ids
from smtrading.risk.models import InterestKind, InterestRate
from smtrading.valuation.service import mark_price

log = logging.getLogger("smtrading.corporate.interest")

JOB = "interest"
AUTHOR = "interest"
_PREC = 60


async def account_id(
    session: AsyncSession, portfolio_id: UUID | None, asset: str, kind: AccountKind
) -> UUID:
    return (
        await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    ).id


async def debit_lines(
    session: AsyncSession, portfolio_id: UUID, ccy: str, amount: Decimal,
    contra: AccountKind,
) -> list[LineSpec]:
    """Lines taking `amount` (> 0) of `ccy` out of the portfolio into the system `contra`
    account: from cash when it covers the whole amount, otherwise added to margin_loan."""
    cash = await account_id(session, portfolio_id, ccy, AccountKind.cash)
    source = cash
    if await get_balance(session, cash) < amount:
        source = await account_id(session, portfolio_id, ccy, AccountKind.margin_loan)
    return [
        LineSpec(source, amount.copy_negate()),
        LineSpec(await account_id(session, None, ccy, contra), amount),
    ]


def daily_interest(base: Decimal, annual_pct: Decimal, ccy: str) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PREC
        return quantize_money(abs(base) * annual_pct / 100 / 360, ccy)


async def _rates(session: AsyncSession) -> dict[tuple[InterestKind, str], Decimal]:
    rows = await session.execute(
        select(InterestRate.kind, InterestRate.key, InterestRate.annual_pct)
    )
    return {(kind, key): pct for kind, key, pct in rows.all()}


async def _balances(session: AsyncSession, portfolio_id: UUID, kind: AccountKind):
    rows = await session.execute(
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == kind,
            AccountBalance.balance < 0,
        )
        .order_by(Account.asset)
    )
    return rows.all()


async def _charges(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID,
    rates: dict[tuple[InterestKind, str], Decimal],
) -> list[tuple[str, Decimal, str]]:
    """(ccy, interest, reason) of every margin loan and short position of the portfolio."""
    out: list[tuple[str, Decimal, str]] = []
    for ccy, balance in await _balances(session, portfolio_id, AccountKind.margin_loan):
        rate = rates.get((InterestKind.loan, ccy), rates.get((InterestKind.loan, "*")))
        if rate is not None:
            out.append((ccy, daily_interest(balance, rate, ccy), f"margin interest {ccy}"))

    shorts = dict(await _balances(session, portfolio_id, AccountKind.position))
    if shorts:
        instruments = {
            i.symbol: i for i in await session.scalars(
                select(Instrument).where(Instrument.symbol.in_(shorts))
            )
        }
        quotes = await qs.get_quotes(sorted(shorts))
        for symbol, qty in shorts.items():
            inst = instruments[symbol]
            rate = rates.get((InterestKind.borrow, inst.asset_class))
            if rate is None:
                continue
            with localcontext() as ctx:
                ctx.prec = _PREC
                value = qty * mark_price(quotes[symbol])
            out.append((
                inst.currency, daily_interest(value, rate, inst.currency),
                f"borrow fee {symbol}",
            ))
    return out


async def _charge_portfolio(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, day: date,
    rates: dict[tuple[InterestKind, str], Decimal],
) -> int:
    if (await lock_portfolio(session, portfolio_id)).status == PortfolioStatus.archived:
        return 0  # archived after the list was read: frozen
    if not await run_job_once(session, JOB, f"{day.isoformat()}:{portfolio_id}"):
        return 0
    journals = 0
    for ccy, amount, reason in await _charges(session, qs, portfolio_id, rates):
        if amount <= 0:
            continue
        await post_journal(
            session, type=JournalType.INTEREST,
            lines=await debit_lines(
                session, portfolio_id, ccy, amount, AccountKind.system_interest
            ),
            author_type="system", author_id=AUTHOR, reason=f"{reason} {day.isoformat()}",
        )
        journals += 1
    return journals


async def run_interest(session: AsyncSession, qs: QuoteSource, day: date) -> int:
    """Charge the interest of `day`; returns the number of journals created.

    Commits once per portfolio. A failing portfolio is rolled back, logged and left for
    the next run; the day is marked done only when none failed.
    """
    key = day.isoformat()
    if await job_done(session, JOB, key):
        return 0
    rates = await _rates(session)
    ids = list(await session.scalars(
        select(Account.portfolio_id).distinct()
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id.in_(live_portfolio_ids()),  # archived: frozen
            or_(Account.kind == AccountKind.margin_loan, Account.kind == AccountKind.position),
            AccountBalance.balance < 0,
        )
        .order_by(Account.portfolio_id)
    ))
    await session.commit()
    total, failed = 0, False
    for portfolio_id in ids:
        try:
            total += await _charge_portfolio(session, qs, portfolio_id, day, rates)
            await session.commit()
        except Exception:
            await session.rollback()
            failed = True
            log.exception("interest of portfolio %s for %s failed", portfolio_id, key)
    if not failed:
        await run_job_once(session, JOB, key)
        await session.commit()
    return total
