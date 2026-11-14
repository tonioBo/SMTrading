"""Pre-trade checks of a real-money portfolio (a portfolio linked to a LIVE_KINDS link).

`live_pre_trade` is run by `risk.checks.pre_trade_check` for such a portfolio, before the
existing risk limits (order notional, position weight, buying power), and BrokerSync
repeats the cash cap and the fuse right before sending (with a fresh broker snapshot):

- link not `armed`/`dry_run` -> LIVE_NOT_ARMED;
- kill switch on -> KILL_SWITCH_ACTIVE for every order, also a reducing one (stricter
  than Ruling R25), except the `live_flatten` orders of «Chiudi tutte le posizioni reali»
  (Ruling R8.2);
- a sell beyond the position -> SHORT_NOT_ALLOWED (a reduce-only order is clamped);
- a buy: `live_cash_needed(estimate, fees)` must not exceed min(the portfolio's cash, the
  broker's cash in the portfolio currency); the broker's cash only from a snapshot read in
  the last SNAPSHOT_MAX_AGE_S (an older one: the portfolio's cash only, BrokerSync checks
  again with fresh data) -> LIVE_CASH_LIMIT;
- fuse (Ruling R8.1): `profile.max_live_orders_per_hour` (default 30) orders already sent
  to the broker by the portfolio in the last 3600 s -> RISK_LIMIT_EXCEEDED and the
  portfolio goes reduce-only (`trip_reduce_only(cause="fuse")`). A reduce-only order is
  not refused by the fuse (an exit must never be blocked; it still trips it); a
  live-flatten order neither (the admin's emergency close, counted only).

Amounts are compared in the portfolio's base currency, converted through EUR with the
rates a MemoQuoteSource prefetched for the portfolio (prefetch_portfolio).
"""

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.base import AccountSnapshot
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.models import LiveState
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import json_safe
from smtrading.fees.calculator import compute_trade_fee
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio
from smtrading.risk.profiles import effective_risk_profile
from smtrading.risk.settings import is_kill_switch_on

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


def snapshot_fresh(link: BrokerLink, now: datetime) -> bool:
    """The link's last account snapshot was read at most SNAPSHOT_MAX_AGE_S ago."""
    at = link.last_snapshot_at
    return (
        at is not None and isinstance(link.last_account_snapshot, dict)
        and (now - at).total_seconds() <= SNAPSHOT_MAX_AGE_S
    )


def snapshot_cash(link: BrokerLink, now: datetime) -> tuple[Decimal, str] | None:
    """(available cash, account currency) of a fresh snapshot, else None."""
    if not snapshot_fresh(link, now):
        return None
    snap = link.last_account_snapshot
    cash, ccy = _dec(snap.get("cash_available")), snap.get("currency")
    if cash is None or not ccy:
        return None
    return cash, str(ccy)


def snapshot_record(snap: AccountSnapshot) -> dict:
    """The stored `BrokerLink.last_account_snapshot` (never the broker's `raw` answer)."""
    return json_safe({
        "account_id": snap.account_id, "account_type": snap.account_type,
        "currency": snap.currency, "cash_available": snap.cash_available,
        "positions_count": snap.positions_count, "open_orders_count": snap.open_orders_count,
        "crypto_enabled": snap.crypto_enabled,
        "order_types": sorted(snap.order_types) if snap.order_types is not None else None,
    })


def account_fingerprint(kind: str, account_id: str) -> str:
    return hashlib.sha256(f"{kind}:{account_id}".encode()).hexdigest()


async def estimate_buy(session: AsyncSession, qs: QuoteSource, *, portfolio: Portfolio,
                       instrument: Instrument, order: Order) -> tuple[Decimal, Decimal]:
    """(value, trade fee) of what is left of a buy order, in the portfolio's base currency:
    at its limit (or stop) price, else at the ask (or last) quote. PROVIDER_UNAVAILABLE
    when no price is known (the cash cap cannot be checked: nothing is sent)."""
    ccy, base = instrument.currency, portfolio.base_currency
    if order.notional is not None:
        value = order.notional
    else:
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
                           "quotazione non disponibile per il controllo della liquidità")
        with localcontext() as ctx:
            ctx.prec = _PREC
            value = (order.qty - (order.filled_qty or Decimal(0))) * price
    rule = resolve_rule(await load_rules(session), portfolio_id=portfolio.id,
                        asset_class=instrument.asset_class, side=str(order.side))
    with localcontext() as ctx:
        ctx.prec = _PREC
        fee = compute_trade_fee(rule, value * await qs.get_fx_rate(ccy, rule.currency))
    return (await to_currency(qs, value, ccy, base),
            await to_currency(qs, fee, rule.currency, base))


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
                          now: datetime) -> None:
    """The cash cap of a real buy (amounts in the portfolio's base currency)."""
    needed = live_cash_needed(est_value, est_fees)
    ours = await portfolio_cash(session, qs, portfolio)
    broker = None
    snap = snapshot_cash(link, now)
    if snap is not None:
        broker = await to_currency(qs, snap[0], snap[1], portfolio.base_currency)
    available = ours if broker is None else min(ours, broker)
    if needed > available:
        ccy = portfolio.base_currency
        details = {"needed": needed, "portfolio_cash": ours, "broker_cash": broker}
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
        held = await held_qty(session, portfolio.id, instrument.symbol)
        if qty is None or qty > held:
            raise SMTError(ErrorCode.SHORT_NOT_ALLOWED, SHORT_MESSAGE,
                           {"held": str(held), "qty": None if qty is None else str(qty)})
    if side == OrderSide.buy:
        await check_live_cash(session, qs, portfolio=portfolio, link=link,
                              est_value=est_value, est_fees=est_fees, now=now)
    await check_live_fuse(session, portfolio=portfolio, now=now, reduce_only=reduce_only,
                          live_flatten=live_flatten, trip=trip, exclude=exclude_order)
