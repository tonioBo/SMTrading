"""«Posizioni aperte»: every open position of the non-archived portfolios with its
protective orders, the distance and the loss to the stop, and per portfolio the risk at
the stops against the daily loss limit. Read-only (nothing is written, not even the
day_open snapshot).

Quantities come from the ledger `position` balances (as in valuation.service), the
average price and `opened_at` from the `positions` projection. A position whose quote
cannot be read (batch, then one by one, then the redis cache: `stale`) keeps `last`,
`value_eur`, `pnl_*`, `distance_to_stop_pct` and `risk_at_stop_eur` null: never 0.
Amounts are converted to EUR with qs.get_fx_rate(ccy, "EUR") (null when the rate fails).

Protective orders follow the protection alarm (`positions.protection.evaluate`): the
resting orders (FILLABLE_STATUSES: a `held` bracket child still waits for its entry and
covers nothing) on the opposite side of the position, not being canceled at the broker (a
modify in flight still protects):
- stop: stop / stop_limit / trailing_stop (price: stop_price, else the trailing level
  from trail_ref_price); with several, the one nearest to the price (highest for a long);
- target: a limit order, preferring the stop's OCO / bracket sibling, else the nearest.
`protection`: "senza stop" without a stop, "copre parziale" when the stops' remaining
quantity is less than the position's, "scade oggi" when the stop or the target is tif day
while the market is open, else "ok".

`risk_at_stop_eur` is what the position loses from the current price to its stop
(>= 0). Per portfolio `risk_at_stop_eur` sums the known ones; `daily_loss_limit_eur` is
the effective risk profile's limit (the lowest of max_daily_loss_eur and
max_daily_loss_pct of today's day_open equity, else of the current equity (cash,
collateral and margin loan plus the positions' values computed here: no second
valuation), else of the latest snapshot), null when unknown or the profile sets none;
`risk_pct_of_limit` = risk / limit * 100. Per portfolio `unprotected` and `partial_cover`
count the "senza stop" and "copre parziale" positions.

With the market data router, a US position has `extended` (marketdata.extended: the latest
pre-market / after-hours price while the regular session is closed, else null) and
`extended_beyond_stop`: that price is at or beyond the stop (below it for a long, above
for a short), the gap risk at the next open. Information only: `last`, the values, the
distance and the risk at the stop stay on the regular session.

`plan_state` (pending | grace | closing, else null) when the position is being closed at
the end of a «piano» (`goals.plans`), with `plan_grace_until` its grace deadline.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import SMTError
from smtrading.common.money import quantize_money
from smtrading.corporate.snapshots import _day_open, local_date
from smtrading.engine.models import (
    FILLABLE_STATUSES,
    BrokerState,
    Order,
    OrderSide,
    OrderType,
    TimeInForce,
)
from smtrading.engine.quotesource import QuoteSource
from smtrading.goals.plans import grace_until, open_closures
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.marketdata.cache import read_quote
from smtrading.marketdata.extended import extended_hours
from smtrading.marketdata.types import Quote
from smtrading.portfolios.mode import admin_mode_fields, portfolio_links
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.risk.models import EquitySnapshot
from smtrading.risk.profiles import effective_risk_profile
from smtrading.valuation.service import mark_price

OK = "ok"
NO_STOP = "senza stop"
PARTIAL = "copre parziale"
EXPIRES_TODAY = "scade oggi"
STOP_TYPES = (OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop)
_PREC = 60
_PCT = Decimal("0.01")


@dataclass
class _Fx:
    qs: QuoteSource
    rates: dict[str, Decimal | None]

    async def eur(self, amount: Decimal | None, ccy: str) -> Decimal | None:
        if amount is None:
            return None
        if ccy not in self.rates:
            try:
                self.rates[ccy] = await self.qs.get_fx_rate(ccy, "EUR")
            except SMTError:
                self.rates[ccy] = None
        rate = self.rates[ccy]
        if rate is None:
            return None
        with localcontext() as ctx:
            ctx.prec = _PREC
            return amount * rate


def _money(v: Decimal | None) -> Decimal | None:
    return None if v is None else quantize_money(v, "EUR")


def _pct(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(_PCT)


async def _quotes(qs: QuoteSource, symbols: list[str], redis) -> tuple[dict[str, Quote], set]:
    """(quotes, stale): batch, then one by one, then the cache; a symbol may be missing."""
    if not symbols:
        return {}, set()
    try:
        return await qs.get_quotes(symbols), set()
    except SMTError:
        pass
    quotes: dict[str, Quote] = {}
    stale: set[str] = set()
    for sym in symbols:
        try:
            quotes.update(await qs.get_quotes([sym]))
            continue
        except SMTError:
            pass
        cached = await read_quote(redis, sym) if redis is not None else None
        if cached is not None:
            quotes[sym] = cached
            stale.add(sym)
    return quotes, stale


def _mark(quote: Quote | None) -> Decimal | None:
    if quote is None:
        return None
    try:
        return mark_price(quote)
    except SMTError:
        return None


def is_active_exit(o: Order) -> bool:
    """A resting order that can fill now and is not being canceled (the alarm's rule)."""
    return o.status in FILLABLE_STATUSES and o.broker_status != BrokerState.CANCEL_REQUESTED


def stop_level(o: Order) -> Decimal | None:
    """The stop price, else (trailing stop) the level from trail_ref_price."""
    return _stop_price(o)


def _stop_price(o: Order) -> Decimal | None:
    if o.stop_price is not None:
        return o.stop_price
    ref = o.trail_ref_price
    if o.type != OrderType.trailing_stop or ref is None:
        return None
    sign = -1 if o.side == OrderSide.sell else 1
    with localcontext() as ctx:
        ctx.prec = _PREC
        if o.trail_pct is not None:
            return ref * (1 + sign * o.trail_pct / 100)
        if o.trail_amount is not None:
            return ref + sign * o.trail_amount
    return None


def _leg(o: Order | None, price: Decimal | None) -> dict | None:
    if o is None:
        return None
    return {"order_id": o.id, "price": price, "tif": o.tif}


def _pick_stop(orders: list[Order], long: bool) -> tuple[Order | None, Decimal | None]:
    stops = [(o, _stop_price(o)) for o in orders if o.type in STOP_TYPES]
    if not stops:
        return None, None
    priced = [s for s in stops if s[1] is not None]
    if not priced:
        return stops[0]
    return (max if long else min)(priced, key=lambda s: s[1])


def _pick_target(orders: list[Order], stop: Order | None, long: bool) -> Order | None:
    limits = [o for o in orders if o.type == OrderType.limit and o.limit_price is not None]
    if stop is not None:
        for o in limits:
            if (stop.oco_group_id is not None and o.oco_group_id == stop.oco_group_id) or (
                    stop.parent_id is not None and o.parent_id == stop.parent_id):
                return o
    if not limits:
        return None
    return (min if long else max)(limits, key=lambda o: o.limit_price)


async def _daily_limit(session, portfolio: Portfolio, now: datetime,
                       equity: Decimal | None) -> Decimal | None:
    profile = await effective_risk_profile(session, portfolio)
    if profile is None:
        return None
    limits: list[Decimal] = []
    if profile.max_daily_loss_eur is not None:
        limits.append(profile.max_daily_loss_eur)
    if profile.max_daily_loss_pct is not None:
        snap = await _day_open(session, portfolio.id, local_date(portfolio, now))
        start: Decimal | None = snap.equity_eur if snap is not None else None
        if start is None:
            start = equity
        if start is None:  # a position without a quote or a rate: the latest snapshot
            start = await session.scalar(
                    select(EquitySnapshot.equity_eur)
                .where(EquitySnapshot.portfolio_id == portfolio.id)
                .order_by(EquitySnapshot.ts.desc()).limit(1))
        if start is not None and start > 0:
            with localcontext() as ctx:
                ctx.prec = _PREC
                limits.append(start * profile.max_daily_loss_pct / 100)
    return min(limits) if limits else None


async def open_positions(
    session: AsyncSession, qs: QuoteSource, now: datetime | None = None, *, redis=None,
    router=None,
) -> dict[str, Any]:
    """{as_of, positions: [...], portfolios: [...]} (Python values; see the module doc).
    `router` (the MarketDataRouter) adds the US extended-hours prices."""
    now = now or datetime.now(UTC)
    portfolios = list(await session.scalars(
        select(Portfolio).where(Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)))
    by_id = {p.id: p for p in portfolios}
    held: dict[uuid.UUID, dict[str, Decimal]] = {}
    money: dict[uuid.UUID, list[tuple[str, Decimal]]] = {}  # cash, collateral, margin loan
    if by_id:
        for pid, kind, asset, balance in (await session.execute(
            select(Account.portfolio_id, Account.kind, Account.asset, AccountBalance.balance)
            .join(AccountBalance, AccountBalance.account_id == Account.id)
            .where(Account.kind.in_([AccountKind.position, AccountKind.cash,
                                     AccountKind.collateral, AccountKind.margin_loan]),
                   Account.portfolio_id.in_(list(by_id)), AccountBalance.balance != 0)
        )).all():
            if kind == AccountKind.position:
                held.setdefault(pid, {})[asset] = balance
            else:
                money.setdefault(pid, []).append((asset, balance))
    symbols = sorted({s for h in held.values() for s in h})
    instruments = {i.symbol: i for i in await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(symbols)))} if symbols else {}
    projections = {(r.portfolio_id, r.symbol): r for r in await session.scalars(
        select(Position).where(Position.portfolio_id.in_(list(held))))} if held else {}
    orders: dict[tuple[uuid.UUID, str], list[Order]] = {}
    if held:
        for o in await session.scalars(
            select(Order).where(Order.portfolio_id.in_(list(held)),
                                Order.status.in_(FILLABLE_STATUSES))
            .order_by(Order.created_at, Order.id)
        ):
            if is_active_exit(o):
                orders.setdefault((o.portfolio_id, o.symbol), []).append(o)
    closing = {(r.portfolio_id, r.symbol): r
               for r in await open_closures(session, list(held))} if held else {}
    quotes, stale = await _quotes(qs, symbols, redis)
    extended = await extended_hours(router, instruments.values(), now) if router else {}
    fx = _Fx(qs, {"EUR": Decimal(1)})
    open_markets: dict[str, bool] = {}

    def is_open(market: str) -> bool:
        if market not in open_markets:
            try:
                open_markets[market] = market_status(market, now).is_open
            except SMTError:
                open_markets[market] = False
        return open_markets[market]

    rows: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    links = await portfolio_links(session, portfolios)
    for p in portfolios:
        risk_sum = Decimal(0)
        value_sum: Decimal | None = Decimal(0)
        pnl_sum: Decimal | None = Decimal(0)
        unprotected = partial = expiring = unknown_risk = 0
        count = 0
        for sym, qty in sorted(held.get(p.id, {}).items()):
            count += 1
            instr = instruments.get(sym)
            ccy = instr.currency if instr else "EUR"
            proj = projections.get((p.id, sym))
            avg = proj.avg_price if proj else None
            long = qty > 0
            mark = _mark(quotes.get(sym))
            opposite = OrderSide.sell if long else OrderSide.buy
            mine = [o for o in orders.get((p.id, sym), []) if o.side == opposite]
            stop, stop_px = _pick_stop(mine, long)
            target = _pick_target(mine, stop, long)
            with localcontext() as ctx:
                ctx.prec = _PREC
                value = qty * mark if mark is not None else None
                pnl = (mark - avg) * qty if mark is not None and avg is not None else None
                pnl_pct = ((mark - avg) / avg * 100 * (1 if long else -1)
                           if mark is not None and avg else None)
                dist = risk = None
                if mark is not None and stop_px is not None and mark != 0:
                    dist = (mark - stop_px) / mark * 100 * (1 if long else -1)
                    risk = max((mark - stop_px) * qty, Decimal(0))
            value_eur, pnl_eur = await fx.eur(value, ccy), await fx.eur(pnl, ccy)
            risk_eur = await fx.eur(risk, ccy)
            ext = extended.get(sym)
            beyond = ext is not None and stop_px is not None and (
                ext["price"] <= stop_px if long else ext["price"] >= stop_px)
            covered = sum((o.qty - (o.filled_qty or Decimal(0)) for o in mine
                           if o.type in STOP_TYPES and o.qty is not None), Decimal(0))
            if stop is None:
                protection = NO_STOP
                unprotected += 1
            elif covered < abs(qty):
                protection = PARTIAL
                partial += 1
            elif instr is not None and is_open(instr.market) and TimeInForce.day in (
                    stop.tif, target.tif if target else None):
                protection = EXPIRES_TODAY
                expiring += 1
            else:
                protection = OK
            if stop is not None:
                if risk_eur is None:
                    unknown_risk += 1
                else:
                    risk_sum += risk_eur
            value_sum = None if value_sum is None or value_eur is None else value_sum + value_eur
            pnl_sum = None if pnl_sum is None or pnl_eur is None else pnl_sum + pnl_eur
            rows.append({
                "portfolio_id": p.id, "portfolio_name": p.name, "symbol": sym,
                "name": instr.name if instr else None,
                "market": instr.market if instr else None, "currency": ccy,
                "side": "long" if long else "short", "qty": qty, "avg_price": avg,
                "opened_at": proj.opened_at if proj else None,
                "last": mark, "stale": sym in stale,
                "value_eur": _money(value_eur), "pnl_eur": _money(pnl_eur),
                "pnl_pct": _pct(pnl_pct),
                "stop": _leg(stop, stop_px),
                "target": _leg(target, target.limit_price if target else None),
                "distance_to_stop_pct": _pct(dist), "risk_at_stop_eur": _money(risk_eur),
                "protection": protection,
                "extended": ext, "extended_beyond_stop": beyond,
                "plan_state": plan.state if (plan := closing.get((p.id, sym))) else None,
                "plan_grace_until": grace_until(plan),
            })
        if not count:
            continue
        equity = value_sum
        for ccy, amount in money.get(p.id, []):
            part = await fx.eur(amount, ccy)
            equity = None if equity is None or part is None else equity + part
        limit = await _daily_limit(session, p, now, equity)
        totals.append({
            "portfolio_id": p.id, "portfolio_name": p.name,
            **admin_mode_fields(p, links.get(p.broker_link_id)),
            "positions": count, "value_eur": _money(value_sum), "pnl_eur": _money(pnl_sum),
            "risk_at_stop_eur": _money(risk_sum), "risk_unknown": unknown_risk,
            "unprotected": unprotected, "partial_cover": partial,
            "expiring_today": expiring,
            "daily_loss_limit_eur": _money(limit),
            "risk_pct_of_limit": _pct(risk_sum / limit * 100) if limit else None,
        })
    return {"as_of": now, "positions": rows, "portfolios": totals}
