"""Portfolio reset (back to a single cash balance, with a fresh day_open: Ruling R2) and
archive/unarchive (Task 13).

Does not commit: the caller commits, then dispatches the queued post-commit effects.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.corporate.actions import KINDS as CORPORATE_KINDS
from smtrading.corporate.actions import catch_up_portfolio
from smtrading.corporate.snapshots import ensure_day_open, local_date
from smtrading.engine.models import OPEN_STATUSES, Order, OrderStatus
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.engine.service import cancel_all_orders, close_all_positions
from smtrading.events import dispatch_post_commit, queue_event
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.ledger.service import LineSpec, get_or_create_account, post_journal
from smtrading.marketdata.types import CalendarEvent
from smtrading.portfolios.locks import lock_live_portfolio, lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import clean_currency, deposit
from smtrading.risk.models import EquitySnapshot, SnapshotKind

log = logging.getLogger("smtrading.admin.portfolio_ops")

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
    portfolio = await lock_live_portfolio(session, portfolio_id)
    saved = (portfolio.status, portfolio.reduce_only_reason, portfolio.reduce_only_until)
    now = datetime.now(UTC)

    canceled = await cancel_all_orders(session, portfolio_id, by="admin")
    still_open = await session.scalar(
        select(Order.id).where(
            Order.portfolio_id == portfolio_id, Order.status.in_(OPEN_STATUSES)
        ).limit(1)
    )
    if still_open is not None:
        # broker cancels are confirmed asynchronously (R3): keep the cancel requests
        # (committed here, the caller rolls back on the error) so BrokerSync sends them
        await session.commit()
        await dispatch_post_commit(session, redis)
        raise SMTError(
            ErrorCode.INVALID_ORDER, "Attendi la conferma degli annullamenti dal broker",
            {"order_id": str(still_open)},
        )
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


# --- archive -------------------------------------------------------------------------

OPEN_ORDERS_MESSAGE = "Annulla prima gli ordini aperti"
BROKER_LINKED_MESSAGE = "Scollega prima il broker"
# frozen portfolios are charged no interest: no borrowing may sit in the archive
BORROWING_MESSAGE = "Chiudi prima le posizioni allo scoperto e rimborsa il prestito a margine"
CALENDAR_UNAVAILABLE_MESSAGE = (
    "Riprova: impossibile verificare gli eventi societari del periodo di archiviazione"
)


async def _audit(session: AsyncSession, method: str, portfolio: Portfolio, by: str, **extra):
    await write_audit(
        session, agent_id=None, method=method,
        params={"portfolio_id": str(portfolio.id), "name": portfolio.name, "by": by, **extra},
        outcome="ok", latency_ms=0,
    )


async def archive_portfolio(session: AsyncSession, portfolio_id: UUID, *, by: str) -> Portfolio:
    """Archive a portfolio: hidden from lists, agents and dashboards, read-only, skipped
    by every job; its ledger, positions and agent assignments stay as they are.

    Refused (INVALID_ORDER) with open orders, a broker link, a short position or a margin
    loan (no interest is charged while archived). Idempotent: an archived
    portfolio is returned unchanged. The current status is kept for `unarchive_portfolio`.
    """
    portfolio = await lock_portfolio(session, portfolio_id)
    if portfolio.status == PortfolioStatus.archived:
        return portfolio
    details = {"portfolio_id": str(portfolio.id)}
    if portfolio.broker_link_id is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, BROKER_LINKED_MESSAGE, details)
    open_order = await session.scalar(
        select(Order.id).where(
            Order.portfolio_id == portfolio.id, Order.status.in_(OPEN_STATUSES)
        ).limit(1)
    )
    if open_order is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, OPEN_ORDERS_MESSAGE, details)
    borrowing = await session.scalar(
        select(Account.id)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio.id,
            Account.kind.in_((AccountKind.position, AccountKind.margin_loan)),
            AccountBalance.balance < 0,
        ).limit(1)
    )
    if borrowing is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, BORROWING_MESSAGE, details)
    prior = str(portfolio.status)
    portfolio.archived_prior_status = prior
    portfolio.status = PortfolioStatus.archived
    portfolio.archived_at = datetime.now(UTC)
    portfolio.archived_by = by
    await session.flush()
    await _audit(session, "admin:portfolio.archive", portfolio, by, prior_status=prior)
    queue_event(session, "portfolio.archived", {"portfolio_id": portfolio.id})
    return portfolio


async def _missed_corporate_events(
    session: AsyncSession, qs: QuoteSource, portfolio: Portfolio, now: datetime
) -> tuple[list[str], dict[date, dict[str, list[CalendarEvent]]]]:
    """(held symbols, events by ex-date and symbol) of the days the portfolio
    spent archived, from the portfolio-local archive date to today. Network I/O: called
    before any lock. A calendar that cannot be read refuses the unarchive."""
    held = list(await session.scalars(
        select(Account.asset)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio.id, Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        ).order_by(Account.asset)
    ))
    if not held or portfolio.archived_at is None:
        return held, {}
    start = local_date(portfolio, portfolio.archived_at)
    end = max(now.astimezone(UTC).date(), local_date(portfolio, now))
    try:
        found = await qs.get_calendar(held, start, end) if start <= end else []
    except Exception as e:  # noqa: BLE001 - never restored without the catch-up
        log.warning("unarchive of %s: calendar unavailable: %s", portfolio.id, e)
        raise SMTError(
            ErrorCode.PROVIDER_UNAVAILABLE, CALENDAR_UNAVAILABLE_MESSAGE,
            {"portfolio_id": str(portfolio.id)},
        ) from None
    by_day: dict[date, dict[str, list[CalendarEvent]]] = defaultdict(lambda: defaultdict(list))
    for e in found:
        if e.kind in CORPORATE_KINDS and e.symbol in held and start <= e.date <= end:
            by_day[e.date][e.symbol].append(e)
    for events in by_day.values():
        for evs in events.values():
            evs.sort(key=lambda e: CORPORATE_KINDS.index(e.kind))
    return held, by_day


async def unarchive_portfolio(
    session: AsyncSession, portfolio_id: UUID, *, qs: QuoteSource, by: str,
    now: datetime | None = None,
) -> Portfolio:
    """Back to the status it had when archived (active when unknown). Idempotent.

    The dividends and splits whose ex-date fell while it was archived (skipped by the
    daily job) are applied first, in the same transaction, with the daily job's per-event
    keys so nothing is applied twice. Refused (PROVIDER_UNAVAILABLE) when the calendar
    cannot be read: never restored without the catch-up.
    """
    now = now or datetime.now(UTC)
    current = await session.get(Portfolio, portfolio_id, populate_existing=True)
    if current is None or current.status != PortfolioStatus.archived:
        return await lock_portfolio(session, portfolio_id)
    archived_at = current.archived_at
    held, by_day = await _missed_corporate_events(session, qs, current, now)

    portfolio = await lock_portfolio(session, portfolio_id)
    if portfolio.status != PortfolioStatus.archived:
        return portfolio
    if portfolio.archived_at != archived_at:  # re-archived meanwhile: read again
        raise SMTError(ErrorCode.INVALID_ORDER, "Riprova: portafoglio modificato",
                       {"portfolio_id": str(portfolio.id)})
    try:
        restored = PortfolioStatus(portfolio.archived_prior_status or "active")
    except ValueError:
        restored = PortfolioStatus.active
    if restored == PortfolioStatus.archived:
        restored = PortfolioStatus.active
    portfolio.status = restored
    portfolio.archived_at = None
    portfolio.archived_by = None
    portfolio.archived_prior_status = None
    await session.flush()
    applied = await catch_up_portfolio(session, portfolio.id, held, by_day)
    await _audit(session, "admin:portfolio.unarchive", portfolio, by, status=str(restored),
                 corporate_journals=applied)
    queue_event(session, "portfolio.unarchived", {"portfolio_id": portfolio.id})
    return portfolio
