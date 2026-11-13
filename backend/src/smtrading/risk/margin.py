"""Margin monitor: warning notifications and forced liquidation (Ruling R8: raw values).

- `check_portfolio_margin`: `margin_level()` "warning" notifies (at most once every 15
  minutes per portfolio) and "maintenance" liquidates.
- `liquidate`: closes positions, most negative unrealized P&L first, with system market
  orders executed at once in a LIQUIDATION journal, until the portfolio is out of
  maintenance; the symbol's open orders (and held bracket children) are canceled.
  Ruling R19: a position whose market is closed or whose quote is missing/stale is
  skipped (listed as `skipped` in the notification) and the next one is tried.
- `margin_sweep`: the worker's periodic check of every margin portfolio (a margin loan
  or a short position), plus the daily loss check of every portfolio holding positions.

Nothing here commits: the caller commits, then dispatches the queued Redis effects
(Ruling R10). The portfolio row is locked FOR UPDATE first (Ruling R11); execute_fill
locks it again, harmlessly, before the chain lock. `redis` is only read: valuations fall
back to cached quotes (Ruling R23); events are queued on the session.

Quotes (I2): a check reads market data through one MemoQuoteSource prefetched for the
portfolio before the first liquidation fill; the fill freezes it, so the revaluations
and the next closes of the same transaction never reach the provider.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.engine.service import cancel_all_orders
from smtrading.events import (
    POST_COMMIT_KEY,
    discard_post_commit,
    dispatch_post_commit,
    notify,
    queue_event,
)
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_portfolio
from smtrading.risk.daily_loss import check_daily_loss
from smtrading.risk.models import Notification
from smtrading.risk.settings import get_stale_threshold
from smtrading.valuation.service import PortfolioValuation, value_portfolio

log = logging.getLogger("smtrading.risk.margin")

WARNING_THROTTLE = timedelta(minutes=15)
MarginResult = Literal["ok", "warning", "liquidated"]


async def _memo(session: AsyncSession, qs: QuoteSource, portfolio_id: UUID) -> MemoQuoteSource:
    memo = MemoQuoteSource.wrap(qs)
    await prefetch_portfolio(session, memo, portfolio_id)
    return memo


def _margin_portfolios_subquery():
    """Portfolio ids with a margin loan (< 0) or a short position."""
    return (
        select(Account.portfolio_id)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.kind.in_((AccountKind.margin_loan, AccountKind.position)),
            AccountBalance.balance < 0,
        )
    )


async def margin_holdings(session: AsyncSession) -> dict[str, set[UUID]]:
    """symbol -> margin portfolios holding a non-zero position in it."""
    rows = await session.execute(
        select(Account.asset, Account.portfolio_id)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.kind == AccountKind.position, AccountBalance.balance != 0,
            Account.portfolio_id.in_(_margin_portfolios_subquery()),
        )
    )
    out: dict[str, set[UUID]] = {}
    for symbol, portfolio_id in rows.all():
        out.setdefault(symbol, set()).add(portfolio_id)
    return out


def _summary(v: PortfolioValuation) -> dict:
    return {
        "equity_eur": v.equity_eur, "exposure_eur": v.exposure_eur,
        "maint_req_eur": v.maint_req_eur, "warn_req_eur": v.warn_req_eur,
    }


async def _warn(
    session: AsyncSession, portfolio_id: UUID, valuation: PortfolioValuation, now: datetime,
    skipped: list[str],
) -> None:
    last = await session.scalar(
        select(Notification.created_at)
        .where(Notification.portfolio_id == portfolio_id, Notification.kind == "margin_warning")
        .order_by(Notification.created_at.desc())
        .limit(1)
    )
    if last is not None and now - last < WARNING_THROTTLE:
        return
    payload = {"portfolio_id": portfolio_id, **_summary(valuation), "skipped": skipped}
    notification = await notify(
        session, portfolio_id=portfolio_id, kind="margin_warning", payload=payload
    )
    notification.created_at = now
    await session.flush()
    queue_event(session, "margin.warning", payload)


async def check_portfolio_margin(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, *, redis=None,
    now: datetime | None = None,
) -> MarginResult:
    now = now or datetime.now(UTC)
    await lock_portfolio(session, portfolio_id)
    qs = await _memo(session, qs, portfolio_id)
    valuation = await value_portfolio(session, qs, portfolio_id, redis=redis)
    level = valuation.margin_level()
    skipped: list[str] = []
    if level == "maintenance":
        closed, skipped, _ = await _liquidate(
            session, qs, portfolio_id, valuation, now, redis=redis
        )
        if closed:
            return "liquidated"
        level = "warning"  # nothing could be closed: at least warn
    if level == "warning":
        await _warn(session, portfolio_id, valuation, now, skipped)
        return "warning"
    return "ok"


async def _closable_quote(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str, now: datetime
) -> Quote | None:
    """The quote to close `symbol` at, or None (Ruling R19: market closed, quote missing,
    stale-marked by the valuation, or older than the asset class stale threshold)."""
    instrument = await session.get(Instrument, symbol)
    if instrument is None or not market_status(instrument.market, now).is_open:
        log.warning("liquidation of %s/%s skipped: market closed", portfolio_id, symbol)
        return None
    try:
        quote = (await qs.get_quotes([symbol])).get(symbol)
    except SMTError as e:
        log.warning("liquidation of %s/%s skipped: no quote (%s)", portfolio_id, symbol, e.code)
        return None
    if quote is None:
        return None
    threshold = await get_stale_threshold(session, instrument.asset_class)
    if (now - quote.ts).total_seconds() > threshold:
        log.warning("liquidation of %s/%s skipped: quote older than %ss",
                    portfolio_id, symbol, threshold)
        return None
    return quote


async def _close_now(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str, held: Decimal,
    quote: Quote, redis=None,
) -> Order | None:
    """A reduce_only system market order for the whole position, filled at once; None
    if it failed."""
    order = Order(
        portfolio_id=portfolio_id, symbol=symbol, type=OrderType.market,
        side=OrderSide.sell if held > 0 else OrderSide.buy, qty=abs(held),
        tif=TimeInForce.gtc, source="system", status=OrderStatus.new, reduce_only=True,
    )
    session.add(order)
    await session.flush()
    effects = session.info.setdefault(POST_COMMIT_KEY, [])
    mark = len(effects)
    try:
        async with session.begin_nested():
            result = await execute_fill(
                session, qs, order.id, quote=quote, journal_type=JournalType.LIQUIDATION,
                redis=redis,
            )
    except SMTError as e:
        del session.info.setdefault(POST_COMMIT_KEY, [])[mark:]
        await session.refresh(order)
        order.status = OrderStatus.rejected
        order.reject_code = str(e.code)
        order.reject_message = (e.message or "")[:512]
        order.updated_at = datetime.now(UTC)
        await session.flush()
        log.error("liquidation of %s/%s failed: %s %s", portfolio_id, symbol, e.code, e.message)
        return None
    if result is None:
        return None
    await cancel_all_orders(session, portfolio_id, symbol, by="system")
    return result.order


async def _liquidate(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID,
    valuation: PortfolioValuation, now: datetime, *, redis=None,
) -> tuple[list[Order], list[str], PortfolioValuation]:
    """(closed orders, skipped symbols, last valuation); the caller holds the lock and
    passes the prefetched memo as `qs`."""
    before = _summary(valuation)
    # unrealized_pnl_eur is unrounded (Ruling R8)
    queue = sorted(valuation.positions, key=lambda p: (p.unrealized_pnl_eur, p.symbol))
    closed: list[Order] = []
    skipped: list[str] = []
    for position in queue:
        if valuation.margin_level() != "maintenance":
            break
        quote = None if position.stale else await _closable_quote(
            session, qs, portfolio_id, position.symbol, now
        )
        order = None if quote is None else await _close_now(
            session, qs, portfolio_id, position.symbol, position.qty, quote, redis
        )
        if order is None:
            skipped.append(position.symbol)
            continue
        closed.append(order)
        try:
            valuation = await value_portfolio(session, qs, portfolio_id, redis=redis)
        except SMTError as e:
            log.warning("liquidation of %s stopped: revaluation failed (%s)",
                        portfolio_id, e.code)
            break
    if closed:
        payload = {
            "portfolio_id": portfolio_id, "before": before, "after": _summary(valuation),
            "orders": [
                {"order_id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty,
                 "price": o.avg_fill_price}
                for o in closed
            ],
            "skipped": skipped,
        }
        await notify(session, portfolio_id=portfolio_id, kind="liquidation", payload=payload)
        queue_event(session, "liquidation", payload)
        log.warning("portfolio %s liquidated: %s", portfolio_id,
                    ", ".join(f"{o.symbol} {o.side} {o.qty}" for o in closed))
    return closed, skipped, valuation


async def liquidate(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, *, redis=None,
    now: datetime | None = None, valuation: PortfolioValuation | None = None,
) -> list[Order]:
    """Close positions, most negative unrealized P&L (EUR) first, until the portfolio is
    out of maintenance; positions whose market is closed or whose quote is stale are
    skipped (Ruling R19). Returns the filled liquidation orders.

    `valuation`, when given, must be taken under the portfolio lock of this transaction.
    """
    await lock_portfolio(session, portfolio_id)
    qs = await _memo(session, qs, portfolio_id)
    if valuation is None:
        valuation = await value_portfolio(session, qs, portfolio_id, redis=redis)
    closed, _, _ = await _liquidate(
        session, qs, portfolio_id, valuation, now or datetime.now(UTC), redis=redis
    )
    return closed


async def check_margin_and_commit(
    sessionmaker: async_sessionmaker[AsyncSession], qs: QuoteSource, redis,
    portfolio_id: UUID, *, now: datetime | None = None,
) -> MarginResult:
    """check_portfolio_margin in its own transaction, then the post-commit effects."""
    async with sessionmaker() as session:
        try:
            result = await check_portfolio_margin(
                session, qs, portfolio_id, redis=redis, now=now
            )
            await session.commit()
        except BaseException:
            await session.rollback()
            discard_post_commit(session)
            raise
        await dispatch_post_commit(session, redis)
        return result


async def margin_sweep(
    sessionmaker: async_sessionmaker[AsyncSession], qs: QuoteSource, redis, *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Margin check of every margin portfolio, daily loss check of every portfolio with
    positions; one transaction per portfolio, errors logged."""
    async with sessionmaker() as session:
        margin_ids = set(await session.scalars(_margin_portfolios_subquery().distinct()))
        holding_ids = set(await session.scalars(
            select(Account.portfolio_id).distinct()
            .join(AccountBalance, AccountBalance.account_id == Account.id)
            .where(
                Account.kind == AccountKind.position, AccountBalance.balance != 0,
                Account.portfolio_id.is_not(None),
            )
        ))
    for portfolio_id in sorted(margin_ids | holding_ids):
        if portfolio_id in margin_ids:
            try:
                await check_margin_and_commit(
                    sessionmaker, qs, redis, portfolio_id, now=clock()
                )
            except Exception:
                log.exception("margin check of portfolio %s failed", portfolio_id)
        try:
            async with sessionmaker() as session:
                portfolio = await lock_portfolio(session, portfolio_id)
                await check_daily_loss(session, qs, portfolio, clock(), redis=redis)
                await session.commit()
        except Exception:
            log.exception("daily loss check of portfolio %s failed", portfolio_id)

