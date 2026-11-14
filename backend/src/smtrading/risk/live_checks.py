"""Pre-trade checks of a real-money portfolio (a portfolio linked to a LIVE_KINDS link).

`live_pre_trade` is run by `risk.checks.pre_trade_check` for such a portfolio, before the
existing risk limits (order notional, position weight, buying power), and BrokerSync
repeats the cash cap and the fuse right before sending (with a fresh broker snapshot):

- link not `armed`/`dry_run` -> LIVE_NOT_ARMED;
- kill switch on -> KILL_SWITCH_ACTIVE for every order, also a reducing one (stricter
  than Ruling R25), except the `live_flatten` orders of «Chiudi tutte le posizioni reali»
  (Ruling R8.2);
- a sell that is not reduce-only: its remaining quantity plus the remaining quantity of
  the portfolio's other open sells on the symbol ahead of it (`open_sells_ahead`) must not
  exceed the position -> SHORT_NOT_ALLOWED (a reduce-only order is clamped);
- a buy: `live_cash_needed(estimate, fees)` must not exceed min(the portfolio's cash, the
  broker's cash in the portfolio currency), both less what the portfolio's other open
  buys ahead of it may still cost (`committed_buys`: value + trade fee + FX fee, the same
  `estimate_buy` for every order); the broker's cash only from a snapshot read in the
  last SNAPSHOT_MAX_AGE_S and not marked stale by a send since (an older one: the
  portfolio's cash only, BrokerSync checks again with fresh data) -> LIVE_CASH_LIMIT;
- fuse (Ruling R8.1): `profile.max_live_orders_per_hour` (default 30) orders already sent
  to the broker by the portfolio in the last 3600 s -> RISK_LIMIT_EXCEEDED and the
  portfolio goes reduce-only (`trip_reduce_only(cause="fuse")`). A reduce-only order is
  not refused by the fuse (an exit must never be blocked; it still trips it); a
  live-flatten order neither (the admin's emergency close, counted only).

"Ahead of" an order (aggregate checks): every other open order of the portfolio already
claimed or sent to the broker, and the never-sent ones created before it (BrokerSync
sends in that order; a new order is behind every existing one). Held bracket children
are not counted (not active); OCO legs are alternatives: a group counts once, with its
largest leg, and the legs of the order's own group are not counted.

Amounts are compared in the portfolio's base currency, converted through EUR with the
rates a MemoQuoteSource prefetched for the portfolio (prefetch_portfolio).
"""

import logging
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.models import LiveState
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str, quantize_money
from smtrading.engine.models import (
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.engine.quotesource import QuoteSource
from smtrading.fees.calculator import FeeRuleData, compute_fx_fee, compute_trade_fee
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import is_kill_switch_on

log = logging.getLogger(__name__)

LIVE_CASH_MARGIN_PCT = Decimal(2)
SNAPSHOT_MAX_AGE_S = 60
FUSE_WINDOW = timedelta(seconds=3600)
DEFAULT_MAX_LIVE_ORDERS_PER_HOUR = 30
SENDABLE_STATES = frozenset({LiveState.armed, LiveState.dry_run})
EUR = "EUR"
_PREC = 60

NOT_ARMED_MESSAGE = "portafoglio reale sospeso: denaro reale non attivo"
KILL_SWITCH_MESSAGE = "kill switch attivo: nessun ordine reale (solo «Chiudi tutte le posizioni reali»)"
SHORT_MESSAGE = "vendita oltre la posizione: niente vendite allo scoperto con denaro reale"
FUSE_MESSAGE = "fusibile: troppi ordini reali nell'ultima ora"


def live_cash_needed(estimate: Decimal, fees: Decimal) -> Decimal:
    """The cash a real buy needs: estimate + fees + LIVE_CASH_MARGIN_PCT % of the estimate."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        return estimate + fees + estimate * LIVE_CASH_MARGIN_PCT / 100


async def live_link_of(session: AsyncSession, portfolio: Portfolio) -> BrokerLink | None:
    """The portfolio's link when it moves real money, else None."""
    if portfolio.broker_link_id is None:
        return None
    link = await session.get(BrokerLink, portfolio.broker_link_id)
    return link if link is not None and link.kind in LIVE_KINDS else None


async def to_currency(qs: QuoteSource, amount: Decimal, ccy: str, target: str) -> Decimal:
    """`amount` of `ccy` in `target`, through EUR (the pairs prefetch_portfolio loads)."""
    if amount == 0 or ccy.upper() == target.upper():
        return amount
    with localcontext() as ctx:
        ctx.prec = _PREC
        eur = amount * await qs.get_fx_rate(ccy, EUR)
        if target.upper() == EUR:
            return eur
        return eur / await qs.get_fx_rate(target, EUR)


async def portfolio_cash(session: AsyncSession, qs: QuoteSource, portfolio: Portfolio) -> Decimal:
    """The portfolio's cash (every currency) in its base currency."""
    rows = (await session.execute(
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == portfolio.id, Account.kind == AccountKind.cash)
    )).all()
    total = Decimal(0)
    for ccy, balance in rows:
        if balance:
            total += await to_currency(qs, balance, ccy, portfolio.base_currency)
    return total


async def held_qty(session: AsyncSession, portfolio_id: UUID, symbol: str) -> Decimal:
    balance = await session.scalar(
        select(AccountBalance.balance)
        .join(Account, Account.id == AccountBalance.account_id)
        .where(Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
               Account.asset == symbol)
    )
    return balance if balance is not None else Decimal(0)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


STALE_KEY = "stale_at"  # in last_account_snapshot: a send happened after this read


def snapshot_fresh(link: BrokerLink, now: datetime) -> bool:
    """The link's last account snapshot was read at most SNAPSHOT_MAX_AGE_S ago and no
    real buy was sent since (`mark_snapshot_stale`)."""
    at = link.last_snapshot_at
    snap = link.last_account_snapshot
    return (
        at is not None and isinstance(snap, dict) and not snap.get(STALE_KEY)
        and (now - at).total_seconds() <= SNAPSHOT_MAX_AGE_S
    )


def mark_snapshot_stale(link: BrokerLink, at: datetime) -> None:
    """After a real buy was sent: the broker cash of the snapshot no longer counts it, so
    the next buy reads the account again (BrokerSync, at most every 5 s)."""
    if isinstance(link.last_account_snapshot, dict):
        link.last_account_snapshot = {**link.last_account_snapshot, STALE_KEY: at.isoformat()}


def snapshot_cash(link: BrokerLink, now: datetime) -> tuple[Decimal, str] | None:
    """(available cash, account currency) of a fresh snapshot, else None."""
    if not snapshot_fresh(link, now):
        return None
    snap = link.last_account_snapshot
    cash, ccy = _dec(snap.get("cash_available")), snap.get("currency")
    if cash is None or not ccy:
        return None
    return cash, str(ccy)


async def fx_fee_eur(qs: QuoteSource, rule: FeeRuleData, *, base: str, ccy: str,
                     notional: Decimal, fee: Decimal, cash_in_ccy: Decimal) -> Decimal:
    """The estimated FX fee (EUR) of a buy of `notional` in `ccy` for a portfolio in
    `base`: the part not covered by the cash already in `ccy` (with the trade fee when
    the rule charges it in `ccy`) is bought with base currency. The pre-trade estimate
    (risk.checks) and the live cash cap use this one formula."""
    if ccy.upper() == base.upper():
        return Decimal(0)
    fee_in_ccy = fee if rule.currency == ccy else Decimal(0)  # paid from cash[ccy] too
    with localcontext() as ctx:
        ctx.prec = _PREC
        shortfall = notional + fee_in_ccy - cash_in_ccy
    if shortfall <= 0:
        return Decimal(0)
    base_to_ccy = await qs.get_fx_rate(base, ccy)
    base_to_eur = await qs.get_fx_rate(base, EUR)
    with localcontext() as ctx:
        ctx.prec = _PREC
        paid = quantize_money(shortfall / base_to_ccy, base, rounding=ROUND_UP)
        return compute_fx_fee(rule, paid, base) * base_to_eur


async def _cash_in(session: AsyncSession, portfolio_id: UUID, ccy: str) -> Decimal:
    balance = await session.scalar(
        select(AccountBalance.balance)
        .join(Account, Account.id == AccountBalance.account_id)
        .where(Account.portfolio_id == portfolio_id, Account.kind == AccountKind.cash,
               Account.asset == ccy)
    )
    return balance if balance is not None else Decimal(0)


async def remaining_buy_value(qs: QuoteSource, instrument: Instrument, order: Order) -> Decimal:
    """What is left of a buy order, in the instrument currency: a notional order's amount
    less what filled; otherwise the open quantity at its limit (or stop) price, else at
    the ask (or last) quote. PROVIDER_UNAVAILABLE when no price is known."""
    filled = order.filled_qty or Decimal(0)
    if order.notional is not None:
        with localcontext() as ctx:
            ctx.prec = _PREC
            done = filled * (order.avg_fill_price or Decimal(0))
        return max(order.notional - done, Decimal(0))
    if order.type in (OrderType.limit, OrderType.stop_limit):
        price = order.limit_price
    elif order.type == OrderType.stop:
        price = order.stop_price
    else:
        try:
            quote = (await qs.get_quotes([instrument.symbol])).get(instrument.symbol)
        except SMTError:
            quote = None
        price = None if quote is None else (quote.ask or quote.last)
    if price is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE,
                       "quotazione non disponibile per il controllo della liquidità",
                       {"symbol": instrument.symbol})
    with localcontext() as ctx:
        ctx.prec = _PREC
        return max(order.qty - filled, Decimal(0)) * price


async def estimate_buy(session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio,
                       instrument: Instrument, order: Order) -> tuple[Decimal, Decimal]:
    """(value, fees) of what is left of a buy order, in the portfolio's base currency;
    fees = trade fee + FX fee (`fx_fee_eur`), as the pre-trade estimate. PROVIDER_UNAVAILABLE
    when no price is known (the cash cap cannot be checked: nothing is sent)."""
    ccy, base = instrument.currency, portfolio.base_currency
    value = quantize_money(await remaining_buy_value(qs, instrument, order), ccy)
    rule = resolve_rule(await load_rules(session), portfolio_id=portfolio.id,
                        asset_class=instrument.asset_class, side=str(OrderSide.buy))
    with localcontext() as ctx:
        ctx.prec = _PREC
        fee = compute_trade_fee(rule, value * await qs.get_fx_rate(ccy, rule.currency))
    fx = await fx_fee_eur(qs, rule, base=base, ccy=ccy, notional=value, fee=fee,
                          cash_in_ccy=await _cash_in(session, portfolio.id, ccy))
    return (await to_currency(qs, value, ccy, base),
            await to_currency(qs, fee, rule.currency, base) + await to_currency(qs, fx, EUR, base))


def _ahead(other: Order, order: Order | None) -> bool:
    """`other` counts before `order` (module docstring): claimed or sent, or created
    earlier. Every existing order is ahead of a new one (`order` None)."""
    if order is None:
        return True
    if _at_broker(other):
        return True
    return (other.created_at, str(other.id)) < (order.created_at, str(order.id))


async def _open_others(session: AsyncSession, portfolio_id: UUID, side: OrderSide, *,
                       symbol: str | None, exclude: UUID | None,
                       order: Order | None) -> list[Order]:
    query = select(Order).where(
        Order.portfolio_id == portfolio_id, Order.side == side,
        Order.status.in_(OPEN_STATUSES - {OrderStatus.held}),
        Order.broker_link_id.is_not(None),
    ).order_by(Order.created_at, Order.id)
    if symbol is not None:
        query = query.where(Order.symbol == symbol)
    for oid in (exclude, order.id if order is not None else None):
        if oid is not None:
            query = query.where(Order.id != oid)
    return [o for o in await session.scalars(query) if _ahead(o, order)]


class _LastPrice:
    """A QuoteSource view for another order's estimate when its quote is missing: the
    last known price of the symbol (a recent fill of the link, `_last_price`) as the ask.
    No margin here: `live_cash_needed` adds the LIVE_CASH_MARGIN_PCT once, as for every
    other estimate."""

    def __init__(self, qs: QuoteSource, symbol: str, price: Decimal) -> None:
        self._qs, self._symbol, self._price = qs, symbol, price

    async def get_quotes(self, symbols, *, max_age_s: int = 5):
        from datetime import UTC

        from smtrading.marketdata.types import Quote

        return {self._symbol: Quote(symbol=self._symbol, bid=None, ask=self._price,
                                    last=self._price, ts=datetime.now(UTC),
                                    provider="last_fill")}

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        return await self._qs.get_fx_rate(base, quote)

    async def get_calendar(self, symbols, start, end):
        return await self._qs.get_calendar(symbols, start, end)


LAST_FILL_MAX_AGE = timedelta(hours=24)
LAST_FILL_MAX_AGE_WEEKEND = timedelta(hours=72)  # on Saturday, Sunday and Monday


def last_fill_max_age(now: datetime) -> timedelta:
    """«One trading day» for the last-fill fallback: 24 h, 72 h when `now` is a Saturday,
    a Sunday or a Monday (UTC), so a Friday fill still counts over the weekend."""
    return LAST_FILL_MAX_AGE_WEEKEND if now.weekday() in (5, 6, 0) else LAST_FILL_MAX_AGE


async def _last_price(session: AsyncSession, portfolio: Portfolio, symbol: str,
                      now: datetime) -> Decimal | None:
    """The price of the portfolio's latest fill of `symbol` on an order routed to its
    (real-money) link, no older than `last_fill_max_age`; paper fills (simulator, or
    another link) never count."""
    from smtrading.engine.models import Fill

    if portfolio.broker_link_id is None:
        return None
    return await session.scalar(
        select(Fill.price).join(Order, Order.id == Fill.order_id)
        .where(Fill.portfolio_id == portfolio.id, Fill.symbol == symbol,
               Order.broker_link_id == portfolio.broker_link_id,
               Fill.created_at >= now - last_fill_max_age(now))
        .order_by(Fill.created_at.desc()).limit(1))


def _at_broker(order: Order) -> bool:
    """Claimed by BrokerSync or sent to the broker (it may fill at any price there)."""
    return (order.broker_order_id is not None or order.broker_sent_at is not None
            or order.broker_status not in (None, BrokerState.TO_SEND))


async def committed_buys(session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio,
                         exclude: UUID | None = None, order: Order | None = None,
                         now: datetime | None = None) -> Decimal:
    """What the portfolio's other open buys ahead of `order` may still cost
    (`live_cash_needed` of `estimate_buy`: value + fees + the 2 % margin, once, base
    currency); `exclude` (a modify) is never counted. Another order's missing quote never
    refuses this one at placement:
    - a never-sent one is priced at the symbol's last fill on this link no older than one
      trading day (`_last_price`);
    - one already claimed/sent to the broker, or with no such fill: on BrokerSync's frozen
      memo PROVIDER_UNAVAILABLE is raised (the new order waits, `_WAIT`); at placement it
      is skipped with a warning (BrokerSync checks again before sending)."""
    from datetime import UTC

    now = now or datetime.now(UTC)
    total = Decimal(0)
    others = await _open_others(session, portfolio.id, OrderSide.buy, symbol=None,
                                exclude=exclude, order=order)
    for other in others:
        instrument = await session.get(Instrument, other.symbol)
        try:
            value, fees = await estimate_buy(session, qs, portfolio=portfolio,
                                             instrument=instrument, order=other)
        except SMTError as e:
            if e.code != ErrorCode.PROVIDER_UNAVAILABLE:
                raise
            last = None if _at_broker(other) else await _last_price(
                session, portfolio, other.symbol, now)
            if last is None:
                if getattr(qs, "frozen", False):
                    raise
                log.warning("open buy %s: no price for the cash cap estimate", other.id)
                continue
            value, fees = await estimate_buy(session, _LastPrice(qs, other.symbol, last),
                                             portfolio=portfolio, instrument=instrument,
                                             order=other)
        total += live_cash_needed(value, fees)
    return total


def _open_qty(order: Order) -> Decimal | None:
    if order.qty is None:
        return None
    return max(order.qty - (order.filled_qty or Decimal(0)), Decimal(0))


async def open_sells_ahead(session: AsyncSession, portfolio_id: UUID, symbol: str, *,
                           exclude: UUID | None = None, order: Order | None = None
                           ) -> Decimal | None:
    """The open quantity of the portfolio's other sells on `symbol` ahead of `order` (an
    OCO group once, its largest leg; `order`'s own group not at all). None when one of
    them has no quantity (a notional sell: the position cannot be compared)."""
    groups: dict[UUID, Decimal] = {}
    total = Decimal(0)
    own = order.oco_group_id if order is not None else None
    for other in await _open_others(session, portfolio_id, OrderSide.sell, symbol=symbol,
                                    exclude=exclude, order=order):
        qty = _open_qty(other)
        if qty is None:
            return None
        if other.oco_group_id is None:
            total += qty
        elif other.oco_group_id != own:
            groups[other.oco_group_id] = max(groups.get(other.oco_group_id, Decimal(0)), qty)
    return total + sum(groups.values(), Decimal(0))


async def check_no_short(session: AsyncSession, *, portfolio: Portfolio, symbol: str,
                         qty: Decimal | None, exclude: UUID | None = None,
                         order: Order | None = None) -> None:
    """A real sell (not reduce-only) of `qty` with the other open sells ahead of it must
    stay within the position -> SHORT_NOT_ALLOWED otherwise."""
    held = await held_qty(session, portfolio.id, symbol)
    others = await open_sells_ahead(session, portfolio.id, symbol, exclude=exclude,
                                    order=order)
    if qty is None or others is None or qty + others > held:
        raise SMTError(ErrorCode.SHORT_NOT_ALLOWED, SHORT_MESSAGE, {
            "held": str(held), "qty": None if qty is None else str(qty),
            "open_sells": None if others is None else str(others)})


async def max_live_orders_per_hour(session: AsyncSession, portfolio: Portfolio) -> int:
    profile = await effective_risk_profile(session, portfolio)
    value = profile.max_live_orders_per_hour if profile is not None else None
    return int(value) if value else DEFAULT_MAX_LIVE_ORDERS_PER_HOUR


async def live_orders_sent(session: AsyncSession, portfolio_id: UUID, now: datetime, *,
                           exclude: UUID | None = None) -> int:
    """Orders of the portfolio sent to the broker in the sliding hour before `now`."""
    query = select(func.count(Order.id)).where(
        Order.portfolio_id == portfolio_id, Order.broker_sent_at.is_not(None),
        Order.broker_sent_at >= now - FUSE_WINDOW,
    )
    if exclude is not None:
        query = query.where(Order.id != exclude)
    return int(await session.scalar(query) or 0)


async def check_live_fuse(session: AsyncSession, *, portfolio: Portfolio, now: datetime,
                          reduce_only: bool, live_flatten: bool, trip: bool = True,
                          exclude: UUID | None = None) -> None:
    """Ruling R8.1 (see the module docstring)."""
    if live_flatten:
        return
    limit = await max_live_orders_per_hour(session, portfolio)
    sent = await live_orders_sent(session, portfolio.id, now, exclude=exclude)
    if sent < limit:
        return
    if trip:
        from smtrading.brokers.live.protect import trip_reduce_only

        await trip_reduce_only(session, portfolio.id, cause="fuse",
                               details={"sent_last_hour": sent, "limit": limit})
    if not reduce_only:
        raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED, FUSE_MESSAGE,
                       {"sent_last_hour": sent, "limit": limit})


async def check_live_cash(session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio,
                          link: BrokerLink, est_value: Decimal, est_fees: Decimal,
                          now: datetime, exclude: UUID | None = None,
                          order: Order | None = None) -> None:
    """The cash cap of a real buy (amounts in the portfolio's base currency): the other
    open buys ahead of it (`committed_buys`) are taken off both cash amounts."""
    needed = live_cash_needed(est_value, est_fees)
    committed = await committed_buys(session, qs, portfolio=portfolio, exclude=exclude,
                                     order=order, now=now)
    ours = await portfolio_cash(session, qs, portfolio)
    broker = None
    snap = snapshot_cash(link, now)
    if snap is not None:
        broker = await to_currency(qs, snap[0], snap[1], portfolio.base_currency)
    available = (ours if broker is None else min(ours, broker)) - committed
    if needed > available:
        ccy = portfolio.base_currency
        details = {"needed": needed, "portfolio_cash": ours, "broker_cash": broker,
                   "committed": committed}
        raise SMTError(
            ErrorCode.LIVE_CASH_LIMIT,
            f"liquidità insufficiente per l'ordine reale: servono "
            f"{decimal_to_str(needed.quantize(Decimal('0.01')))} {ccy}, disponibili "
            f"{decimal_to_str(available.quantize(Decimal('0.01')))} {ccy}",
            {k: None if v is None else str(v) for k, v in details.items()},
        )


async def check_live_state(session: AsyncSession, link: BrokerLink, *,
                           live_flatten: bool) -> None:
    """The link can send (armed or dry run) and the kill switch is off (unless flatten)."""
    if link.live_state not in SENDABLE_STATES:
        raise SMTError(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
    if not live_flatten and await is_kill_switch_on(session):
        raise SMTError(ErrorCode.KILL_SWITCH_ACTIVE, KILL_SWITCH_MESSAGE)


async def live_pre_trade(
    session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio, link: BrokerLink,
    instrument: Instrument, side: OrderSide, qty: Decimal | None, notional: Decimal | None,
    est_value: Decimal, est_fees: Decimal, reduce_only: bool, live_flatten: bool,
    now: datetime, trip: bool = True, exclude_order: UUID | None = None,
) -> None:
    """Raises SMTError when a real order must be refused (module docstring). `est_value`
    and `est_fees` are in the portfolio's base currency; `qty` is the order quantity (from
    the price estimate for a notional order). `trip=False` (a preview, a modify) never
    puts the portfolio in reduce-only; `exclude_order` is not counted by the fuse."""
    await check_live_state(session, link, live_flatten=live_flatten)
    if side == OrderSide.sell and not reduce_only:
        await check_no_short(session, portfolio=portfolio, symbol=instrument.symbol, qty=qty,
                             exclude=exclude_order)
    if side == OrderSide.buy:
        await check_live_cash(session, qs, portfolio=portfolio, link=link,
                              est_value=est_value, est_fees=est_fees, now=now,
                              exclude=exclude_order)
    await check_live_fuse(session, portfolio=portfolio, now=now, reduce_only=reduce_only,
                          live_flatten=live_flatten, trip=trip, exclude=exclude_order)
