import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.base import NO_VALUE

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.events import queue_event
from smtrading.ledger.models import Account, AccountBalance, AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import RiskProfile
from smtrading.risk.profiles import DEFAULT_RISK_PROFILE_NAME

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# -- live reduce-only guard --------------------------------------------------------------
# A real-money portfolio put in reduce-only by an anomaly (brokers.live.protect) has a
# `reduce_only_reason` starting with LIVE_REASON_PREFIX. Only resume_live_portfolio (TOTP)
# may make it active again: every other path (the admin PATCH, the midnight reset of the
# daily loss, an unarchive, a broker unlock) is refused, by `set_portfolio_status` and, for
# the paths that assign the attributes directly, by the before_flush check below.
LIVE_REASON_PREFIX = "live_"
LIVE_RESUME_MESSAGE = "riprendi il portafoglio reale con «Ho verificato, riprendi» (TOTP)"
_LIVE_RESUME_KEY = "smt_live_resume_ok"


def is_live_reason(reason: str | None) -> bool:
    return bool(reason) and reason.startswith(LIVE_REASON_PREFIX)


def _live_resume_refused(portfolio_id) -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, LIVE_RESUME_MESSAGE,
                    {"field": "status", "portfolio_id": str(portfolio_id)})


def set_portfolio_status(
    session: AsyncSession, portfolio: Portfolio, status: PortfolioStatus, *,
    allow_live: bool = False,
) -> None:
    """Sets the status; `active` also clears the reduce-only reason and end. Making active a
    portfolio blocked for a live anomaly is FORBIDDEN unless `allow_live` (only
    brokers.live.service.resume_live_portfolio passes it, after the TOTP)."""
    if (status == PortfolioStatus.active and portfolio.status != PortfolioStatus.active
            and is_live_reason(portfolio.reduce_only_reason)):
        if not allow_live:
            raise _live_resume_refused(portfolio.id)
        session.sync_session.info.setdefault(_LIVE_RESUME_KEY, set()).add(portfolio.id)
    portfolio.status = status
    if status == PortfolioStatus.active:
        portfolio.reduce_only_reason = None
        portfolio.reduce_only_until = None


_UNKNOWN = object()


def _before(attr) -> tuple[bool, object]:
    """(changed, value before the change) of an attribute; the value is _UNKNOWN when it
    was changed without being loaded first (expired)."""
    hist = attr.history
    if not hist.has_changes():
        return False, attr.loaded_value
    return True, hist.deleted[0] if hist.deleted else _UNKNOWN


def _stored(session: Session, portfolio_id) -> tuple[object, object]:
    """(status, reduce_only_reason) as stored, read on the flush's connection (no autoflush)."""
    row = session.connection().execute(
        select(Portfolio.status, Portfolio.reduce_only_reason)
        .where(Portfolio.id == portfolio_id)
    ).first()
    return (row.status, row.reduce_only_reason) if row is not None else (None, None)


@event.listens_for(Session, "before_flush")
def _guard_live_reduce_only(session: Session, flush_context, instances) -> None:
    """Refuses a flush that makes active, or takes the live reason away from, a portfolio
    blocked for a live anomaly (reason `live_*`), unless it went through
    set_portfolio_status(..., allow_live=True). Replacing the reason with another one (a
    margin lock, a daily loss) or clearing it in a first flush is refused too."""
    allowed: set = session.info.get(_LIVE_RESUME_KEY, set())
    for obj in session.dirty:
        if not isinstance(obj, Portfolio) or obj.id in allowed:
            continue
        state = inspect(obj)
        status_changed, old_status = _before(state.attrs.status)
        reason_changed, old_reason = _before(state.attrs.reduce_only_reason)
        if not status_changed and not reason_changed:
            continue
        if old_reason is _UNKNOWN or old_reason is NO_VALUE or (
                status_changed and old_status in (_UNKNOWN, NO_VALUE)):
            old_status, old_reason = _stored(session, obj.id)
        if not is_live_reason(old_reason):
            continue
        new_reason = state.attrs.reduce_only_reason.loaded_value
        new_status = state.attrs.status.loaded_value
        if reason_changed and not is_live_reason(new_reason):
            raise _live_resume_refused(obj.id)
        if (status_changed and new_status == PortfolioStatus.active
                and old_status != PortfolioStatus.active):
            raise _live_resume_refused(obj.id)
    session.info.pop(_LIVE_RESUME_KEY, None)


def clean_currency(currency: str) -> str:
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
    ccy = clean_currency(currency)
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
    # Lock order: portfolio row first, then the chain lock inside post_journal.
    await lock_portfolio(session, portfolio_id)
    cash = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset=ccy, kind=AccountKind.cash
    )
    equity = await get_or_create_account(
        session, portfolio_id=None, asset=ccy, kind=AccountKind.system_equity
    )
    delta = value if sign > 0 else value.copy_negate()
    queue_event(session, "portfolio.cash", {
        "portfolio_id": portfolio_id, "type": "deposit" if sign > 0 else "withdrawal",
        "amount": value, "currency": ccy,
    })
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
    author_id: str | None = None, risk_profile_id: UUID | None = None,
) -> Portfolio:
    """A new portfolio with its initial deposit; without `risk_profile_id` it gets the
    seeded Default risk profile (if present)."""
    ccy = clean_currency(base_currency)
    _check_finite(initial_cash, "initial_cash")
    # Checked before quantizing: -0.001 would otherwise round to -0.00 and pass.
    if initial_cash < 0:
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, "initial cash must not be negative",
            {"initial_cash": str(initial_cash)},
        )
    cash_amount = _quantize(initial_cash, ccy, "initial_cash")
    if await session.scalar(select(Portfolio.id).where(Portfolio.name == name)) is not None:
        raise SMTError(
            ErrorCode.INVALID_ORDER, "portfolio name already exists",
            {"field": "name", "name": name},
        )
    if risk_profile_id is None:
        risk_profile_id = await session.scalar(
            select(RiskProfile.id).where(RiskProfile.name == DEFAULT_RISK_PROFILE_NAME)
        )
    portfolio = Portfolio(
        name=name, base_currency=ccy, leverage=leverage, slippage_bps=slippage_bps,
        timezone=timezone, risk_profile_id=risk_profile_id,
    )
    session.add(portfolio)
    await session.flush()
    queue_event(session, "portfolio.created", {"portfolio_id": portfolio.id, "name": name})
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
