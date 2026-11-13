"""Portfolio reset: back to a single cash balance, with a fresh day_open (Ruling R2).

Does not commit: the caller commits, then dispatches the queued post-commit effects.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.corporate.snapshots import ensure_day_open, local_date
from smtrading.engine.models import Order, OrderStatus
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.engine.service import cancel_all_orders, close_all_positions
from smtrading.events import queue_event
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import PortfolioStatus
from smtrading.portfolios.service import clean_currency, deposit
from smtrading.risk.models import EquitySnapshot, SnapshotKind

_MONEY_KINDS = (AccountKind.cash, AccountKind.collateral, AccountKind.margin_loan)
CLOSED_MARKETS_MESSAGE = "posizioni aperte su mercati chiusi"


@dataclass
class ResetResult:
    portfolio_id: UUID
    canceled_orders: int
    closing_orders: list[Order] = field(default_factory=list)
    reset_journal_id: UUID | None = None
    deposit_journal_id: UUID | None = None


async def _held_markets(session: AsyncSession, portfolio_id: UUID) -> dict[str, str | None]:
    """symbol -> market of every non-zero position (None: no such instrument)."""
    rows = await session.execute(
        select(Account.asset, Instrument.market)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .outerjoin(Instrument, Instrument.symbol == Account.asset)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        )
        .order_by(Account.asset)
    )
    return dict(rows.all())


async def _zero_money_accounts(
    session: AsyncSession, portfolio_id: UUID, *, reason: str, author_id: str | None
) -> UUID | None:
    """One RESET journal bringing every cash, collateral and margin_loan balance to zero
    against system_equity (one equity line per asset); None when all are zero already."""
    rows = (await session.execute(
        select(Account.id, Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind.in_(_MONEY_KINDS),
            AccountBalance.balance != 0,
        )
        .order_by(Account.asset, Account.kind)
    )).all()
    if not rows:
        return None
    lines: list[LineSpec] = []
    per_asset: dict[str, Decimal] = {}
    for account_id, asset, balance in rows:
        lines.append(LineSpec(account_id, balance.copy_negate()))
        per_asset[asset] = per_asset.get(asset, Decimal(0)) + balance
    for asset, total in sorted(per_asset.items()):
        if total != 0:  # e.g. collateral and margin_loan of one currency can cancel out
            equity = await get_or_create_account(
                session, portfolio_id=None, asset=asset, kind=AccountKind.system_equity
            )
            lines.append(LineSpec(equity.id, total))
    journal = await post_journal(
        session, type=JournalType.RESET, lines=lines, author_type="admin",
        author_id=author_id, reason=reason,
    )
    return journal.id


async def reset_portfolio(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, new_cash: Decimal,
    currency: str = "EUR", *, reason: str, author_id: str | None, redis=None,
) -> ResetResult:
    """Reset a portfolio:

    1. cancel its open orders;
    2. with open positions on a closed market, or on an unknown instrument, or in a
       portfolio linked to a broker -> INVALID_ORDER (nothing is changed);
    3. close every position at market (source "admin"); any refusal aborts the reset;
    4. zero every cash, collateral and margin_loan balance with one RESET journal;
    5. deposit `new_cash` in `currency` (nothing when it is 0);
    6. replace today's day_open snapshot with one taken after the reset (Ruling R2), so the
       daily loss restarts from the new equity.

    The status (and reduce_only reason/until) is the same afterwards: a locked portfolio
    is unlocked only for the closing orders, and a daily-loss reduce_only set by the
    closing fills themselves is undone.
    """
    reason = (reason or "").strip()
    if not reason:
        raise SMTError(ErrorCode.INVALID_ORDER, "motivazione obbligatoria", {"field": "reason"})
    if new_cash < 0:
        raise SMTError(
            ErrorCode.INVALID_AMOUNT, "new_cash must not be negative", {"field": "new_cash"}
        )
    currency = clean_currency(currency)
    memo = MemoQuoteSource.wrap(qs)
    portfolio = await lock_portfolio(session, portfolio_id)
    saved = (portfolio.status, portfolio.reduce_only_reason, portfolio.reduce_only_until)
    now = datetime.now(UTC)

    canceled = await cancel_all_orders(session, portfolio_id, by="admin")
    held = await _held_markets(session, portfolio_id)
    unknown = sorted(s for s, m in held.items() if m is None)
    if unknown:
        raise SMTError(
            ErrorCode.INVALID_ORDER, "posizioni su strumenti sconosciuti", {"symbols": unknown}
        )
    if held and portfolio.broker_link_id is not None:
        # closes go to the broker (Ruling R3) and fill asynchronously: they cannot be
        # part of this transaction
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            "portafoglio collegato a un broker: chiudi prima le posizioni (gli ordini vanno"
            " al broker) e ripeti il reset quando sono eseguiti",
            {"symbols": sorted(held)},
        )
    closed = sorted(s for s, m in held.items() if not market_status(m, now).is_open)
    if closed:
        raise SMTError(ErrorCode.INVALID_ORDER, CLOSED_MARKETS_MESSAGE, {"symbols": closed})

    # needed by the day_open valuation, after the closing fills froze the memo
    await memo.get_fx_rate(currency, "EUR")
    result = ResetResult(portfolio_id=portfolio_id, canceled_orders=canceled)
    if held:
        if portfolio.status == PortfolioStatus.locked:
            portfolio.status = PortfolioStatus.active
            await session.flush()
        result.closing_orders = await close_all_positions(
            session, memo, portfolio_id, source="admin", redis=redis
        )
        for order in result.closing_orders:
            if order.status == OrderStatus.rejected:
                raise SMTError(
                    order.reject_code or ErrorCode.INVALID_ORDER,
                    order.reject_message or "closing order rejected",
                    {"symbol": order.symbol, "order_id": str(order.id)},
                )
        still_open = await _held_markets(session, portfolio_id)
        if still_open:
            raise SMTError(
                ErrorCode.INVALID_ORDER, "posizioni non chiuse", {"symbols": sorted(still_open)}
            )

    result.reset_journal_id = await _zero_money_accounts(
        session, portfolio_id, reason=reason, author_id=author_id
    )
    if new_cash > 0:
        result.deposit_journal_id = (await deposit(
            session, portfolio_id, new_cash, currency, author_id=author_id, reason=reason
        )).id

    portfolio = await lock_portfolio(session, portfolio_id)  # refreshed after the closes
    portfolio.status, portfolio.reduce_only_reason, portfolio.reduce_only_until = saved
    # taken after the deposit: it is in the new day_open equity, not a cash flow since it
    snapshot_at = datetime.now(UTC)
    await session.execute(
        delete(EquitySnapshot).where(
            EquitySnapshot.portfolio_id == portfolio_id,
            EquitySnapshot.kind == SnapshotKind.day_open,
            EquitySnapshot.local_date == local_date(portfolio, snapshot_at),
        )
    )
    await ensure_day_open(session, memo, portfolio, snapshot_at, redis=redis)
    queue_event(session, "portfolio.reset", {
        "portfolio_id": portfolio_id, "new_cash": new_cash, "currency": currency,
        "canceled_orders": canceled, "closed_positions": len(result.closing_orders),
    })
    return result
