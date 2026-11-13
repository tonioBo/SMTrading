"""Read-side portfolio queries shared by the MCP tools and the admin API: the journals
touching a portfolio (with only its own lines) and the day P&L."""

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.money import quantize_money
from smtrading.corporate.snapshots import local_date
from smtrading.engine.quotesource import QuoteSource
from smtrading.ledger.models import Account, Journal, JournalLine, JournalType
from smtrading.portfolios.models import Portfolio
from smtrading.risk.daily_loss import net_cash_flows_eur
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.valuation.service import PortfolioValuation

_PREC = 60
_Q4 = Decimal("0.0001")


async def list_portfolio_journals(
    session: AsyncSession, portfolio_id: UUID, *, limit: int,
    types: list[JournalType] | None = None, start: datetime | None = None,
    end: datetime | None = None, before_seq: int | None = None, with_audit: bool = False,
) -> list[dict[str, Any]]:
    """Journals with at least one line on the portfolio's accounts, newest first (seq
    descending), at most `limit`; `start`/`end` inclusive on created_at, `before_seq` the
    exclusive cursor.

    Each item: {id, seq, type, created_at, reason, order_id, lines: [{asset, kind,
    quantity, unit_price, price_ccy}]} (only the portfolio's own lines, in posting order);
    `with_audit` adds author_type, author_id, reversed_journal_id and reversed_by (the id
    of the journal's REVERSAL, if any). Values are Python objects (Decimal, UUID, ...).
    """
    own_lines = (
        select(JournalLine.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.journal_id == Journal.id, Account.portfolio_id == portfolio_id)
    )
    stmt = select(Journal).where(own_lines.exists()).order_by(Journal.seq.desc()).limit(limit)
    if types is not None:
        stmt = stmt.where(Journal.type.in_(types))
    if start is not None:
        stmt = stmt.where(Journal.created_at >= start)
    if end is not None:
        stmt = stmt.where(Journal.created_at <= end)
    if before_seq is not None:
        stmt = stmt.where(Journal.seq < before_seq)
    journals = list(await session.scalars(stmt))
    if not journals:
        return []

    ids = [j.id for j in journals]
    lines: dict[UUID, list[dict]] = {i: [] for i in ids}
    rows = await session.execute(
        select(JournalLine, Account.asset, Account.kind)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.journal_id.in_(ids), Account.portfolio_id == portfolio_id)
        .order_by(JournalLine.id)
    )
    for line, asset, kind in rows.all():
        lines[line.journal_id].append({
            "asset": asset, "kind": kind, "quantity": line.quantity,
            "unit_price": line.unit_price, "price_ccy": line.price_ccy,
        })
    reversed_by: dict[UUID, UUID] = {}
    if with_audit:
        reversed_by = dict((await session.execute(
            select(Journal.reversed_journal_id, Journal.id)
            .where(Journal.reversed_journal_id.in_(ids))
        )).all())

    out = []
    for j in journals:
        item: dict[str, Any] = {
            "id": j.id, "seq": j.seq, "type": j.type, "created_at": j.created_at,
            "reason": j.reason, "order_id": j.order_id,
        }
        if with_audit:
            item.update(
                author_type=j.author_type, author_id=j.author_id,
                reversed_journal_id=j.reversed_journal_id, reversed_by=reversed_by.get(j.id),
            )
        item["lines"] = lines[j.id]
        out.append(item)
    return out


async def today_day_open(
    session: AsyncSession, portfolio: Portfolio, now: datetime
) -> EquitySnapshot | None:
    """Today's (portfolio-local) day_open snapshot, if taken; never created here."""
    return await session.scalar(
        select(EquitySnapshot).where(
            EquitySnapshot.portfolio_id == portfolio.id,
            EquitySnapshot.kind == SnapshotKind.day_open,
            EquitySnapshot.local_date == local_date(portfolio, now),
        )
    )


async def day_pnl(
    session: AsyncSession, qs: QuoteSource, portfolio: Portfolio, valuation: PortfolioValuation
) -> tuple[Decimal | None, Decimal | None]:
    """(EUR in cents, % to 4 decimals): equity - today's day_open equity - net deposits
    since it (external cash flows are not P&L, as in risk.daily_loss); (None, None)
    without a day_open, % None when the day_open equity is not positive."""
    day_open = await today_day_open(session, portfolio, valuation.ts)
    if day_open is None:
        return None, None
    flows = await net_cash_flows_eur(session, qs, portfolio.id, day_open.ts, valuation.ts)
    pct = None
    with localcontext() as c:
        c.prec = _PREC
        raw = valuation.equity_raw - day_open.equity_eur - flows
        if day_open.equity_eur > 0:
            pct = (raw / day_open.equity_eur * 100).quantize(_Q4, rounding=ROUND_HALF_EVEN)
    return quantize_money(raw, "EUR"), pct
