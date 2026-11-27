"""Broker fees charged apart from the orders (found on 25/11: Alpaca's regulatory fees of
an MSFT sale, 0,07 $, posted the night after as account activities, booked 0 here).

`book_broker_fees` (scheduler, every FEE_SYNC_INTERVAL_S) reads, for every enabled
paper/demo link whose adapter lists them (`BrokerAdapter.fee_activities`: Alpaca; eToro,
Trading 212 and the others none, their adapter is not even called), the fees of the last
LOOKBACK_DAYS, with no transaction open, then books each one in its own transaction:

- idempotent: the fee is claimed in job_runs (`broker_fee / <link>:<activity id>`) in the
  same transaction as its journals and its warning, so a second pass or a restart never
  books or tells it twice. Whatever is not booked is claimed all the same (never guessed
  again later) and told to the owner: one Telegram message (category system, never the
  agents' feed, so a hidden portfolio's fee is never shown to them);
- accepted: USD fees of the sub types REG, TAF, CAT (FEES_HANDLED); the amount rounded
  once, half up, to the cent (one that rounds to 0 is claimed and logged);
- attribution: the broker fills of the link's orders on the fee's trading day (New York
  date of the fill's `created_at`: the broker's own fill time is not stored per fill) of
  every portfolio, archived ones included, of equities only (crypto fees are booked by
  rate), the dust write-offs left out (`write_off_dust`, journal reason «fee residue»);
  a REG / TAF fee (on sells) over the sells, a CAT fee over every fill; split by notional
  in cents (largest remainder). A share falling on an archived portfolio is not booked
  (warning: archived portfolios are frozen);
- no fill that day: charged to the link's only portfolio ever (linked now or with an
  order on the link) when it is not archived; otherwise not booked (warning). A fee is
  never moved to a portfolio that did not trade that day but the link's only one;
- booking: one FEE journal per portfolio out of its USD cash into system_fees, with the
  order and fill of its largest fill of the day (the «Strategie» costs count it). Never a
  margin loan and no currency conversion (the FX funding of execution is an order's,
  with its fee rule and quotes): a portfolio whose USD cash does not cover its share is
  not charged (warning).

The cash reconciliation (reconciliation.compare) counts only a shortfall beyond
max(0,01, 0,1 % of our cash): a few cents of fees never booked were «ok» there, booked
they leave our cash equal to the broker's.
"""

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.base import BrokerAdapter, BrokerFee
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import get_adapter
from smtrading.common.money import quantize_money
from smtrading.engine.models import Fill, Order, OrderSide
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.jobs.runs import run_job_once
from smtrading.ledger.models import AccountKind, Journal, JournalType
from smtrading.ledger.service import LineSpec, get_balance, get_or_create_account, post_journal
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.telegram.outbox import enqueue

log = logging.getLogger("smtrading.brokers.fees")

JOB = "broker_fee"
AUTHOR = "broker_fee"
LOOKBACK_DAYS = 7
FEE_SYNC_INTERVAL_S = 900
SELL_FEES = frozenset({"REG", "TAF"})  # charged on the sells of the day
FEES_HANDLED = SELL_FEES | {"CAT"}  # CAT: on every trade of the day
FEE_CURRENCY = "USD"
DUST_REASON = "fee residue"  # the journal reason of a dust write-off (engine.execution)
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
        .join(Instrument, Instrument.symbol == Fill.symbol)
        .join(Journal, Journal.id == Fill.journal_id)
        .where(Order.broker_link_id == link_id, Fill.source == "broker",
               Instrument.asset_class != AssetClass.crypto,
               or_(Journal.reason.is_(None), Journal.reason != DUST_REASON),
               Fill.created_at >= start, Fill.created_at < end)
        .order_by(Fill.created_at, Fill.id)
    )).all()
    if (fee.kind or "").upper() in SELL_FEES:
        rows = [r for r in rows if r.side == OrderSide.sell]
    return rows


async def _only_portfolio(session: AsyncSession, link_id: UUID) -> UUID | None:
    """The link's only portfolio ever (linked now, or with an order on the link)."""
    ids = set(await session.scalars(union(
        select(Portfolio.id).where(Portfolio.broker_link_id == link_id),
        select(Order.portfolio_id).where(Order.broker_link_id == link_id))))
    return ids.pop() if len(ids) == 1 else None


async def _shares(session: AsyncSession, link_id: UUID, fee: BrokerFee, amount: Decimal
                  ) -> dict[UUID, tuple[Decimal, UUID | None, UUID | None]] | None:
    """portfolio → (amount, order id, fill id); None: not attributable."""
    rows = await _day_fills(session, link_id, fee)
    if not rows:
        only = await _only_portfolio(session, link_id)
        return None if only is None else {only: (amount, None, None)}
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
    return {pid: (share, largest[pid][1], largest[pid][2])
            for pid, share in split_cents(amount, fee.currency, weights).items()}


def _money(v: Decimal) -> str:
    return f"{v:.2f}".replace(".", ",")


async def _warn(session: AsyncSession, link_name: str, fee: BrokerFee, amount: Decimal,
                reasons: list[str]) -> None:
    """The owner's Telegram message (category system, never the agents' feed)."""
    log.warning("broker fee %s of %s (%s %s, %s) not booked: %s", fee.id, link_name,
                fee.amount, fee.currency, fee.trade_date, "; ".join(reasons))
    text = (f"⚠️ Commissione del broker non registrata ({link_name}): "
            f"{_money(amount)} {fee.currency} {fee.kind or ''} del "
            f"{fee.trade_date:%d/%m/%Y}.\n" + "\n".join(f"• {r}" for r in reasons)
            + "\nDa fare: verificare la cassa del portafoglio con il conto del broker.")
    await enqueue(session, source="system", category="system", level="warning",
                  text=text[:3800])


async def _charge(session: AsyncSession, pid: UUID, amount: Decimal, fee: BrokerFee,
                  order_id: UUID | None, fill_id: UUID | None) -> str | None:
    """One FEE journal out of the portfolio's USD cash; the reason it was not, else None."""
    portfolio = await lock_portfolio(session, pid)
    if portfolio.status == PortfolioStatus.archived:
        return f"{_money(amount)} {fee.currency} su «{portfolio.name}», archiviato"
    cash = await get_or_create_account(session, portfolio_id=pid, asset=fee.currency,
                                       kind=AccountKind.cash)
    if await get_balance(session, cash.id) < amount:
        return (f"{_money(amount)} {fee.currency} su «{portfolio.name}»: cassa in "
                f"{fee.currency} insufficiente")
    system = await get_or_create_account(session, portfolio_id=None, asset=fee.currency,
                                         kind=AccountKind.system_fees)
    await post_journal(
        session, type=JournalType.FEE,
        lines=[LineSpec(cash.id, amount.copy_negate()), LineSpec(system.id, amount)],
        author_type="system", author_id=AUTHOR, order_id=order_id, fill_id=fill_id,
        reason=f"commissione del broker {fee.kind} {fee.trade_date.isoformat()}: "
               f"{fee.description} [{fee.id}]",
    )
    return None


async def book_fee(session: AsyncSession, link_id: UUID, fee: BrokerFee) -> int:
    """Books one broker fee in the session's transaction (the caller commits); the
    journals written (0: booked before, or not booked: see the module docstring)."""
    if not await run_job_once(session, JOB, f"{link_id}:{fee.id}"):
        return 0
    link_name = await session.scalar(
        select(BrokerLink.name).where(BrokerLink.id == link_id)) or str(link_id)
    amount = fee.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if fee.currency != FEE_CURRENCY or (fee.kind or "").upper() not in FEES_HANDLED:
        await _warn(session, link_name, fee, amount,
                    [f"tipo non gestito ({fee.kind or '—'}, {fee.currency})"])
        return 0
    if amount <= 0:
        log.info("broker fee %s of %s rounds to 0 (%s %s): nothing booked", fee.id,
                 link_name, fee.amount, fee.currency)
        return 0
    shares = await _shares(session, link_id, fee, amount)
    if shares is None:
        await _warn(session, link_name, fee, amount, [
            "nessuna esecuzione quel giorno e più portafogli sul collegamento"])
        return 0
    journals, reasons = 0, []
    for pid in sorted(shares, key=str):  # a fixed lock order
        share, order_id, fill_id = shares[pid]
        reason = await _charge(session, pid, share, fee, order_id, fill_id)
        if reason is None:
            journals += 1
        else:
            reasons.append(reason)
    if reasons:
        await _warn(session, link_name, fee, amount, reasons)
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
