import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.ledger.models import Account, AccountBalance, AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.portfolios.models import Portfolio

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _clean_currency(currency: str) -> str:
    ccy = currency.strip().upper()
    if not _CURRENCY_RE.match(ccy):
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, "currency must be a 3-letter ISO code",
            {"field": "currency", "value": currency},
        )
    return ccy


def _check_finite(amount: Decimal, field: str) -> None:
    if not amount.is_finite():
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, f"{field} must be a finite number",
            {"field": field, "value": str(amount)},
        )


def _quantize(amount: Decimal, ccy: str, field: str) -> Decimal:
    try:
        return quantize_money(amount, ccy)
    except InvalidOperation:
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, f"{field} cannot be represented",
            {"field": field, "value": str(amount)},
        ) from None


def _clean_amount(amount: Decimal, currency: str) -> tuple[Decimal, str]:
    ccy = _clean_currency(currency)
    _check_finite(amount, "amount")
    return _quantize(amount, ccy, "amount"), ccy


async def _post_cash_move(
    session: AsyncSession, portfolio_id: UUID, amount: Decimal, currency: str, *,
    type: JournalType, sign: int, author_id: str | None, reason: str | None,
) -> Journal:
    """Move `amount` between the portfolio cash account and system_equity.

    sign=+1 credits the portfolio cash (deposit), sign=-1 debits it (withdrawal).
    """
    value, ccy = _clean_amount(amount, currency)
    if value <= 0:
        raise SMTError(ErrorCode.INVALID_AMOUNT, "amount must be positive", {"amount": str(amount)})
    if await session.get(Portfolio, portfolio_id) is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)})
    cash = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=ccy, kind=AccountKind.cash
    )
    equity = await get_or_create_account(
        session, portfolio_id=None, asset=ccy, kind=AccountKind.system_equity
    )
    delta = value if sign > 0 else value.copy_negate()
    return await post_journal(
        session, type=type,
        lines=[LineSpec(cash.id, delta), LineSpec(equity.id, delta.copy_negate())],
        author_type="admin", author_id=author_id, reason=reason,
    )


async def deposit(
    session: AsyncSession, portfolio_id: UUID, amount: Decimal, currency: str, *,
    author_id: str | None, reason: str | None = None,
) -> Journal:
    return await _post_cash_move(
        session, portfolio_id, amount, currency, type=JournalType.DEPOSIT, sign=1,
        author_id=author_id, reason=reason,
    )


async def withdraw(
    session: AsyncSession, portfolio_id: UUID, amount: Decimal, currency: str, *,
    author_id: str | None, reason: str | None = None,
) -> Journal:
    return await _post_cash_move(
        session, portfolio_id, amount, currency, type=JournalType.WITHDRAWAL, sign=-1,
        author_id=author_id, reason=reason,
    )


async def create_portfolio(
    session: AsyncSession, *, name: str, initial_cash: Decimal, base_currency: str = "EUR",
    leverage: Decimal = Decimal(1), slippage_bps: int = 0, timezone: str = "Europe/Rome",
    author_id: str | None = None,
) -> Portfolio:
    ccy = _clean_currency(base_currency)
    _check_finite(initial_cash, "initial_cash")
    # Checked before quantizing: -0.001 would otherwise round to -0.00 and pass.
    if initial_cash < 0:
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, "initial cash must not be negative",
            {"initial_cash": str(initial_cash)},
        )
    cash_amount = _quantize(initial_cash, ccy, "initial_cash")
    if await session.scalar(select(Portfolio.id).where(Portfolio.name == name)) is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, "portfolio name already exists", {"name": name})
    portfolio = Portfolio(
        name=name, base_currency=ccy, leverage=leverage, slippage_bps=slippage_bps,
        timezone=timezone,
    )
    session.add(portfolio)
    await session.flush()
    if cash_amount > 0:
        await deposit(
            session, portfolio.id, cash_amount, ccy, author_id=author_id,
            reason="initial deposit",
        )
    return portfolio


async def get_cash_balances(session: AsyncSession, portfolio_id: UUID) -> dict[str, Decimal]:
    rows = await session.execute(
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == portfolio_id, Account.kind == AccountKind.cash)
    )
    return {asset: balance for asset, balance in rows.all()}
