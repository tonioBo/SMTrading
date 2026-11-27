"""Broker fees charged apart from the orders (found on 25/11: Alpaca's regulatory fees of
an MSFT sale, 0,07 $, posted the night after as account activities, booked 0 here).

`book_broker_fees` (scheduler, every FEE_SYNC_INTERVAL_S) reads, for every enabled
paper/demo link whose adapter lists them (`BrokerAdapter.fee_activities`: Alpaca; eToro,
Trading 212 and the others none, their adapter is not even called), the fees of the last
LOOKBACK_DAYS, with no transaction open, then books each one in its own transaction:

- idempotent: the fee is claimed in job_runs (`broker_fee / <link>:<activity id>`) in the
  same transaction as its journals, so a second pass or a restart never books it twice;
- attribution: the broker fills of the link's orders on the fee's trading day (New York
  date of the fill) of its non-archived portfolios; a REG / TAF fee (on sells) over the
  sells, else (CAT, any other) over every fill; split by notional, in cents (largest
  remainder). Each portfolio's share is one FEE journal out of its cash (or margin loan
  when the cash does not cover it, like interest) into system_fees, with the order and
  fill of its largest fill of the day (the «Strategie» costs count it);
- no fill that day: charged to the link's only non-archived portfolio (no order); with
  several it is not booked (a warning; claimed all the same, never guessed).

The cash reconciliation (reconciliation.compare) counts only a shortfall beyond
max(0,01, 0,1 % of our cash): a few cents of fees never booked were «ok» there, booked
they leave our cash equal to the broker's.
"""

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.base import BrokerAdapter, BrokerFee
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import get_adapter
from smtrading.common.money import quantize_money
from smtrading.corporate.interest import debit_lines
from smtrading.engine.models import Fill, Order, OrderSide
from smtrading.jobs.runs import run_job_once
from smtrading.ledger.models import AccountKind, JournalType
from smtrading.ledger.service import post_journal
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus

log = logging.getLogger("smtrading.brokers.fees")

JOB = "broker_fee"
AUTHOR = "broker_fee"
LOOKBACK_DAYS = 7
FEE_SYNC_INTERVAL_S = 900
SELL_FEES = frozenset({"REG", "TAF"})  # charged on the sells of the day
MARKET_TZ = ZoneInfo("America/New_York")
_PREC = 60


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), MARKET_TZ)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def split_cents(amount: Decimal, ccy: str, weights: dict[UUID, Decimal]) -> dict[UUID, Decimal]:
    """`amount` split by `weights` in the currency's smallest unit (largest remainder, ties
    by key): the shares add up to `amount` exactly; zero shares left out."""
    unit = Decimal(1).scaleb(quantize_money(Decimal(1), ccy).as_tuple().exponent)
    total = sum(weights.values(), Decimal(0))
    if total <= 0:
        return {}
    with localcontext() as ctx:
        ctx.prec = _PREC
        units = int(amount / unit)
        raw = {k: units * w / total for k, w in weights.items()}
        floor = {k: int(v.to_integral_value(rounding=ROUND_DOWN)) for k, v in raw.items()}
        left = units - sum(floor.values())
        for k in sorted(raw, key=lambda k: (-(raw[k] - floor[k]), str(k)))[:left]:
            floor[k] += 1
        return {k: n * unit for k, n in floor.items() if n > 0}


async def _day_fills(session: AsyncSession, link_id: UUID, fee: BrokerFee):
    start, end = _day_bounds(fee.trade_date)
    rows = (await session.execute(
        select(Fill.id, Fill.order_id, Fill.portfolio_id, Fill.side, Fill.qty, Fill.price)
        .join(Order, Order.id == Fill.order_id)
        .join(Portfolio, Portfolio.id == Fill.portfolio_id)
        .where(Order.broker_link_id == link_id, Fill.source == "broker",
               Portfolio.status != PortfolioStatus.archived,
               Fill.created_at >= start, Fill.created_at < end)
        .order_by(Fill.created_at, Fill.id)
    )).all()
    if (fee.kind or "").upper() in SELL_FEES:
        sells = [r for r in rows if r.side == OrderSide.sell]
        rows = sells or rows
    return rows


async def _shares(session: AsyncSession, link_id: UUID, fee: BrokerFee
                  ) -> dict[UUID, tuple[Decimal, UUID | None, UUID | None]] | None:
    """portfolio → (amount, order id, fill id); None: not attributable."""
    rows = await _day_fills(session, link_id, fee)
    if not rows:
        pids = list(await session.scalars(
            select(Portfolio.id).where(Portfolio.broker_link_id == link_id,
                                       Portfolio.status != PortfolioStatus.archived)))
        if len(pids) != 1:
            return None
        return {pids[0]: (quantize_money(fee.amount, fee.currency), None, None)}
    weights: dict[UUID, Decimal] = defaultdict(Decimal)
    largest: dict[UUID, tuple[Decimal, UUID, UUID]] = {}
    with localcontext() as ctx:
        ctx.prec = _PREC
        for r in rows:
            notional = abs(r.qty * r.price)
            weights[r.portfolio_id] += notional
            if r.portfolio_id not in largest or notional > largest[r.portfolio_id][0]:
                largest[r.portfolio_id] = (notional, r.order_id, r.id)
    if sum(weights.values(), Decimal(0)) <= 0:
        weights = defaultdict(Decimal, dict.fromkeys(weights, Decimal(1)))
    return {pid: (amount, largest[pid][1], largest[pid][2])
            for pid, amount in split_cents(fee.amount, fee.currency, weights).items()}


async def book_fee(session: AsyncSession, link_id: UUID, fee: BrokerFee) -> int:
    """Books one broker fee in the session's transaction (the caller commits); the
    journals written (0: booked before, or not attributable)."""
    if not await run_job_once(session, JOB, f"{link_id}:{fee.id}"):
        return 0
    shares = await _shares(session, link_id, fee)
    if shares is None:
        log.warning("broker fee %s of link %s (%s %s, %s) not attributed: no fill that day "
                    "and several portfolios", fee.id, link_id, fee.amount, fee.currency,
                    fee.trade_date)
        return 0
    journals = 0
    for pid in sorted(shares, key=str):  # a fixed lock order
        amount, order_id, fill_id = shares[pid]
        if amount <= 0:
            continue
        await lock_portfolio(session, pid)
        await post_journal(
            session, type=JournalType.FEE,
            lines=await debit_lines(session, pid, fee.currency, amount,
                                    AccountKind.system_fees),
            author_type="system", author_id=AUTHOR, order_id=order_id, fill_id=fill_id,
            reason=f"commissione del broker {fee.kind or ''} {fee.trade_date.isoformat()}: "
                   f"{fee.description} [{fee.id}]".replace("  ", " "),
        )
        journals += 1
    return journals


def _lists_fees(adapter: BrokerAdapter) -> bool:
    return type(adapter).fee_activities is not BrokerAdapter.fee_activities


async def book_broker_fees(sessionmaker: async_sessionmaker[AsyncSession], http, *,
                           now: datetime | None = None) -> int:
    """Every enabled paper/demo link (real-money ones, with a `live_state`, go through
    their live gate and are not read here): its broker fees of the last LOOKBACK_DAYS
    booked once. Returns the journals written; a link's failure is logged and skipped."""
    since = (now or datetime.now(UTC)).astimezone(MARKET_TZ).date() - timedelta(
        days=LOOKBACK_DAYS)
    async with sessionmaker() as session:
        link_ids = list(await session.scalars(
            select(BrokerLink.id)
            .where(BrokerLink.enabled.is_(True), BrokerLink.live_state.is_(None))
            .order_by(BrokerLink.name)))
    written = 0
    for link_id in link_ids:
        try:
            async with sessionmaker() as session:
                adapter = await get_adapter(session, link_id, http)
                await session.commit()
            if not _lists_fees(adapter):
                continue
            fees = await adapter.fee_activities(since)  # no transaction open
        except Exception as e:  # noqa: BLE001  (one link's failure never stops the others)
            log.warning("broker fees of link %s not read: %s", link_id, e)
            continue
        for fee in fees:
            try:
                async with sessionmaker() as session:
                    n = await book_fee(session, link_id, fee)
                    await session.commit()
                written += n
                if n:
                    log.info("broker fee %s of link %s booked: %s %s", fee.id, link_id,
                             fee.amount, fee.currency)
            except Exception:
                log.exception("broker fee %s of link %s not booked", fee.id, link_id)
    return written
