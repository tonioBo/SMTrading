"""Daily loss limit: equity against the `day_open` snapshot; a breach sets reduce_only.

The portfolio stays reduce_only until the next local midnight (`reset_daily`) or until
an admin unlocks it.
"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import SMTError
from smtrading.corporate.snapshots import ensure_day_open
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import notify
from smtrading.ledger.models import Account, AccountKind, Journal, JournalLine, JournalType
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.profiles import effective_risk_profile
from smtrading.valuation.service import value_portfolio

log = logging.getLogger("smtrading.risk.daily_loss")

DAILY_LOSS_REASON = "daily_loss"
_PREC = 60


def next_local_midnight(portfolio: Portfolio, now: datetime) -> datetime:
    tz = ZoneInfo(portfolio.timezone)
    tomorrow = now.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0), tzinfo=tz)


async def net_cash_flows_eur(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, since: datetime,
    until: datetime,
) -> Decimal:
    """Deposits minus withdrawals on the portfolio's cash accounts with
    since < journal.created_at <= until, in EUR (Ruling R20)."""
    rows = await session.execute(
        select(Account.asset, func.sum(JournalLine.quantity))
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.cash,
            Journal.type.in_((JournalType.DEPOSIT, JournalType.WITHDRAWAL)),
            Journal.created_at > since, Journal.created_at <= until,
        )
        .group_by(Account.asset)
    )
    total = Decimal(0)
    for ccy, amount in rows.all():
        rate = await qs.get_fx_rate(ccy, "EUR")
        with localcontext() as ctx:
            ctx.prec = _PREC
            total += amount * rate
    return total


async def check_daily_loss(
    session: AsyncSession, qs: QuoteSource, portfolio: Portfolio, now: datetime
) -> bool:
    """True if this call has just set the portfolio reduce_only for a daily loss.

    loss = day_open equity - equity now + net deposits since day_open (Ruling R20):
    external cash flows are neither losses nor gains.

    The caller holds the portfolio row lock (FOR UPDATE). A loss equal to the limit
    counts as reached. Only `active` portfolios are checked.
    """
    if portfolio.status != PortfolioStatus.active:
        return False
    profile = await effective_risk_profile(session, portfolio)
    if profile is None or (
        profile.max_daily_loss_pct is None and profile.max_daily_loss_eur is None
    ):
        return False
    day_open = await ensure_day_open(session, qs, portfolio, now)
    valuation = await value_portfolio(session, qs, portfolio.id)
    flows = await net_cash_flows_eur(session, qs, portfolio.id, day_open.ts, now)
    with localcontext() as ctx:
        ctx.prec = _PREC
        start = day_open.equity_eur
        loss = start - valuation.equity_raw + flows
        limits: list[Decimal] = []
        if profile.max_daily_loss_eur is not None:
            limits.append(profile.max_daily_loss_eur)
        if profile.max_daily_loss_pct is not None and start > 0:
            limits.append(start * profile.max_daily_loss_pct / 100)
        if not limits or loss <= 0 or loss < min(limits):
            return False

    until = next_local_midnight(portfolio, now)
    portfolio.status = PortfolioStatus.reduce_only
    portfolio.reduce_only_reason = DAILY_LOSS_REASON
    portfolio.reduce_only_until = until
    await notify(
        session, portfolio_id=portfolio.id, kind="daily_loss",
        payload={
            "portfolio_id": portfolio.id, "day_open_equity_eur": start,
            "equity_eur": valuation.equity_eur, "net_cash_flows_eur": flows,
            "loss_eur": loss,
            "max_daily_loss_pct": profile.max_daily_loss_pct,
            "max_daily_loss_eur": profile.max_daily_loss_eur, "reduce_only_until": until,
        },
    )
    log.warning("portfolio %s is reduce_only until %s: daily loss %s EUR",
                portfolio.id, until.isoformat(), loss)
    return True


async def check_daily_loss_safely(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, now: datetime
) -> bool:
    """check_daily_loss in a SAVEPOINT after a fill: best-effort, errors are logged.

    Locks the portfolio row first (Ruling R11); the caller commits.
    """
    try:
        async with session.begin_nested():
            portfolio = (
                await session.execute(
                    select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            return await check_daily_loss(session, qs, portfolio, now)
    except SMTError as e:
        log.warning("daily loss check of %s skipped: %s %s", portfolio_id, e.code, e.message)
    except Exception:
        log.exception("daily loss check of %s failed", portfolio_id)
    return False


async def reset_daily(session: AsyncSession, now: datetime) -> int:
    """Return to `active` the portfolios blocked for a daily loss whose block has ended."""
    result = await session.execute(
        update(Portfolio)
        .where(
            Portfolio.status == PortfolioStatus.reduce_only,
            Portfolio.reduce_only_reason == DAILY_LOSS_REASON,
            Portfolio.reduce_only_until <= now,
        )
        .values(status=PortfolioStatus.active, reduce_only_reason=None, reduce_only_until=None)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount
