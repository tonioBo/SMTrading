"""Order service: place, preview, cancel, modify, get and list orders; brackets, OCO, closes.

Transactions and side effects (Rulings R10, R11):
- Nothing here commits and there is no `redis` parameter. Redis side effects (order
  events, `track`/`untrack` with source "orders", and whatever execute_fill queues) are
  queued on the session; callers `await session.commit()` and then
  `await events.dispatch_post_commit(session, redis)`.
- Every mutating call locks the portfolio row FOR UPDATE first, then order rows; the chain
  lock is only taken later, inside post_journal.
- Malformed requests (price/trail parameters that do not match the type, non-positive
  values, notional on a non-market order, ...) raise INVALID_ORDER and store nothing.
- A request refused by pre_trade_check is stored as `rejected` (reject_code,
  reject_message) and flushed, then SMTError is raised with that code. Callers commit
  even on SMTError so the rejection is kept.
- Immediate executions run in a SAVEPOINT: on SMTError the savepoint and the effects it
  queued are discarded, and the order is stored as `rejected` with that code.

Market closed: market orders are `queued` (the processor executes them at the open);
other types rest as `pending` and are only evaluated once the market is open, because a
queued order is executed at market. While the market is closed the quote age is not
checked at placement; it is checked again at execution.
"""

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.triggers import OrderView, TriggerAction, evaluate
from smtrading.events import POST_COMMIT_KEY, queue_event, queue_track, queue_untrack
from smtrading.instruments.market_hours import MarketStatus, market_status
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import get_instrument, validate_price, validate_quantity
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.marketdata.types import CalendarEvent, Quote
from smtrading.portfolios.models import Portfolio
from smtrading.risk.checks import CheckResult, pre_trade_check
from smtrading.risk.settings import get_default_spread_bps

OPEN_STATUSES = frozenset({
    OrderStatus.new, OrderStatus.held, OrderStatus.queued, OrderStatus.pending,
    OrderStatus.triggered,
})
MODIFIABLE = frozenset({OrderStatus.pending, OrderStatus.queued, OrderStatus.held})
_SELF_EXPIRING_MARKETS = ("CRYPTO", "FX")  # DAY orders expire at local midnight
_MAX_VALUE = Decimal("1e20")
_PRICE_PARAMS: dict[OrderType, tuple[str, ...]] = {
    OrderType.market: (),
    OrderType.limit: ("limit_price",),
    OrderType.stop: ("stop_price",),
    OrderType.stop_limit: ("limit_price", "stop_price"),
    OrderType.trailing_stop: (),
}
_PARAMS = ("qty", "notional", "limit_price", "stop_price", "trail_pct", "trail_amount")


@dataclass
class PlaceOrderRequest:
    portfolio_id: UUID
    symbol: str
    side: OrderSide
    type: OrderType
    tif: TimeInForce = TimeInForce.gtc
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_pct: Decimal | None = None
    trail_amount: Decimal | None = None
    client_order_id: str | None = None
    agent_id: str | None = None
    source: str = "agent"


def _invalid(message: str, **details) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {k: str(v) for k, v in details.items()})


def _positive(name: str, value) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _invalid(f"{name} must be a finite decimal", **{name: value})
    if not (0 < value < _MAX_VALUE):
        raise _invalid(f"{name} must be positive", **{name: value})


def _validate_request(req: PlaceOrderRequest) -> PlaceOrderRequest:
    """Normalized copy of `req`, or INVALID_ORDER for a malformed request."""
    try:
        req = dataclasses.replace(
            req, side=OrderSide(req.side), type=OrderType(req.type), tif=TimeInForce(req.tif)
        )
    except ValueError as e:
        raise _invalid(str(e)) from None
    if (req.qty is None) == (req.notional is None):
        raise _invalid("exactly one of qty and notional is required")
    if req.notional is not None and req.type != OrderType.market:
        raise _invalid("notional is only allowed for market orders")
    for name in _PARAMS:
        value = getattr(req, name)
        if value is not None:
            _positive(name, value)
    needed = _PRICE_PARAMS[req.type]
    for name in ("limit_price", "stop_price"):
        given = getattr(req, name) is not None
        if name in needed and not given:
            raise _invalid(f"{req.type} order requires {name}")
        if name not in needed and given:
            raise _invalid(f"{name} is not allowed for a {req.type} order")
    if req.type == OrderType.trailing_stop:
        if (req.trail_pct is None) == (req.trail_amount is None):
            raise _invalid("trailing_stop requires exactly one of trail_pct and trail_amount")
        if req.trail_pct is not None and req.trail_pct >= 100:
            raise _invalid("trail_pct must be below 100", trail_pct=req.trail_pct)
    elif req.trail_pct is not None or req.trail_amount is not None:
        raise _invalid(f"trail parameters are not allowed for a {req.type} order")
    return req


class _IgnoreQuoteAge:
    """QuoteSource view with quotes stamped `now`: no STALE_QUOTE while a market is closed."""

    def __init__(self, qs: QuoteSource) -> None:
        self._qs = qs

    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        quotes = await self._qs.get_quotes(symbols, max_age_s=max_age_s)
        ts = datetime.now(UTC)
        return {s: dataclasses.replace(q, ts=ts) for s, q in quotes.items()}

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        return await self._qs.get_fx_rate(base, quote)

    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        return await self._qs.get_calendar(symbols, start, end)


async def _lock_portfolio(session: AsyncSession, portfolio_id: UUID) -> Portfolio:
    portfolio = (
        await session.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if portfolio is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    return portfolio


async def _lock_order(session: AsyncSession, order_id: UUID) -> Order:
    """Portfolio row first, then the order row (Ruling R11)."""
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        raise SMTError(ErrorCode.NOT_FOUND, "order not found", {"order_id": str(order_id)})
    await _lock_portfolio(session, portfolio_id)
    return (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _try_quote(qs: QuoteSource, symbol: str) -> Quote | None:
    try:
        return (await qs.get_quotes([symbol])).get(symbol)
    except SMTError:
        return None


async def _bid_ask(
    session: AsyncSession, instrument: Instrument, quote: Quote
) -> tuple[Decimal, Decimal]:
    spread = await get_default_spread_bps(session, instrument.asset_class)
    return effective_bid_ask(QuoteView(quote.bid, quote.ask, quote.last, quote.ts), spread)


def _day_expiry(
    instrument: Instrument, portfolio: Portfolio, status: MarketStatus, now: datetime
) -> datetime | None:
    if instrument.market in _SELF_EXPIRING_MARKETS:
        tz = ZoneInfo(portfolio.timezone)
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        return datetime.combine(tomorrow, time(0), tzinfo=tz).astimezone(UTC)
    if status.next_close is not None:  # open: this session; closed: the next session
        return status.next_close
    if status.next_open is not None:
        return market_status(
            instrument.market, status.next_open + timedelta(minutes=1)
        ).next_close
    return None


def _same_params(order: Order, req: PlaceOrderRequest, symbol: str) -> bool:
    return (
        order.symbol == symbol and order.side == req.side and order.type == req.type
        and order.tif == req.tif
        and all(getattr(order, name) == getattr(req, name) for name in _PARAMS)
    )


@dataclass
class _Admission:
    req: PlaceOrderRequest
    portfolio: Portfolio
    instrument: Instrument
    market: MarketStatus
    now: datetime
    existing: Order | None = None
    check: CheckResult | None = None


async def _run_check(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, portfolio: Portfolio,
    instrument: Instrument, market: MarketStatus,
) -> CheckResult:
    return await pre_trade_check(
        session, qs if market.is_open else _IgnoreQuoteAge(qs),
        portfolio=portfolio, instrument=instrument, side=req.side, qty=req.qty,
        notional=req.notional, order_type=req.type, limit_price=req.limit_price,
        stop_price=req.stop_price,
    )


async def _admit(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, now: datetime | None
) -> _Admission:
    """Validated request + locked portfolio + idempotency lookup + risk check (no writes)."""
    now = now or datetime.now(UTC)
    req = _validate_request(req)
    portfolio = await _lock_portfolio(session, req.portfolio_id)
    instrument = await get_instrument(session, req.symbol)
    market = market_status(instrument.market, now)
    adm = _Admission(req, portfolio, instrument, market, now)
    if req.client_order_id is not None:
        # under the portfolio lock: a concurrent duplicate waits and then sees this row
        existing = await session.scalar(
            select(Order).where(
                Order.portfolio_id == portfolio.id,
                Order.client_order_id == req.client_order_id,
            )
        )
        if existing is not None:
            if not _same_params(existing, req, instrument.symbol):
                raise _invalid(
                    "client_order_id reused with different parameters",
                    client_order_id=req.client_order_id, order_id=existing.id,
                )
            adm.existing = existing
            return adm
    adm.check = await _run_check(session, qs, req, portfolio, instrument, market)
    return adm


def _reject(order: Order, code: ErrorCode | str, message: str | None) -> None:
    order.status = OrderStatus.rejected
    order.reject_code = str(code)
    order.reject_message = (message or "")[:512]
    order.updated_at = datetime.now(UTC)


async def _persist(session: AsyncSession, adm: _Admission, **extra) -> Order:
    req, check = adm.req, adm.check
    order = Order(
        id=uuid.uuid4(), portfolio_id=req.portfolio_id, agent_id=req.agent_id,
        client_order_id=req.client_order_id, symbol=adm.instrument.symbol, side=req.side,
        type=req.type, tif=req.tif, qty=req.qty, notional=req.notional,
        limit_price=req.limit_price, stop_price=req.stop_price, trail_pct=req.trail_pct,
        trail_amount=req.trail_amount, filled_qty=Decimal(0), status=OrderStatus.new,
        source=req.source, **extra,
    )
    if check is not None and not check.ok:
        _reject(order, check.code, check.message)
    elif req.tif == TimeInForce.day:
        order.expires_at = _day_expiry(adm.instrument, adm.portfolio, adm.market, adm.now)
    session.add(order)
    await session.flush()
    return order


def _rejection_error(order: Order) -> SMTError:
    return SMTError(
        ErrorCode(order.reject_code), order.reject_message or "order rejected",
        {"order_id": str(order.id)},
    )


async def _fill_now(session: AsyncSession, qs: QuoteSource, order: Order, quote: Quote) -> None:
    effects = session.info.setdefault(POST_COMMIT_KEY, [])
    mark = len(effects)
    try:
        async with session.begin_nested():
            await execute_fill(session, qs, order.id, quote=quote)
    except SMTError as e:
        del session.info.setdefault(POST_COMMIT_KEY, [])[mark:]
        await session.refresh(order)
        _reject(order, e.code, e.message)
        await session.flush()


def _view(order: Order) -> OrderView:
    return OrderView(
        type=order.type, side=order.side, status=order.status,
        limit_price=order.limit_price, stop_price=order.stop_price,
        trail_pct=order.trail_pct, trail_amount=order.trail_amount,
        trail_ref_price=order.trail_ref_price,
    )


async def _evaluate_now(
    session: AsyncSession, qs: QuoteSource, order: Order, quote: Quote,
    bid: Decimal, ask: Decimal,
) -> None:
    # at most two steps: a stop_limit first triggers, then may fill as a limit
    for _ in range(2):
        result = evaluate(_view(order), bid, ask)
        if result.new_trail_ref is not None:
            order.trail_ref_price = result.new_trail_ref
        if result.action == TriggerAction.fill:
            await session.flush()
            await _fill_now(session, qs, order, quote)
            return
        if result.action == TriggerAction.trigger_to_market:
            order.status = OrderStatus.triggered
            await session.flush()
            await _fill_now(session, qs, order, quote)
            return
        if result.action != TriggerAction.trigger_to_limit:
            return
        order.status = OrderStatus.triggered


async def _activate(session: AsyncSession, qs: QuoteSource, order: Order, adm: _Admission) -> None:
    """Move a freshly stored `new` order to queued/pending, executing it when it can."""
    if order.status != OrderStatus.new:
        return
    instrument = adm.instrument
    quote = await _try_quote(qs, instrument.symbol)
    if order.type == OrderType.market:
        if not adm.market.is_open:
            order.status = OrderStatus.queued
        elif quote is None:
            _reject(order, ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")
        else:
            await _fill_now(session, qs, order, quote)
        await session.flush()
        return
    order.status = OrderStatus.pending
    if quote is not None:
        bid, ask = await _bid_ask(session, instrument, quote)
        if order.type == OrderType.trailing_stop:
            order.trail_ref_price = bid if order.side == OrderSide.sell else ask
        if adm.market.is_open:
            await _evaluate_now(session, qs, order, quote, bid, ask)
    await session.flush()


def _track_open(session: AsyncSession, orders: list[Order]) -> None:
    if any(o.status in OPEN_STATUSES for o in orders):
        queue_track(session, orders[0].symbol, "orders")


async def _place(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, now: datetime | None
) -> tuple[Order, bool]:
    """(order, created); a stored rejection is returned, not raised."""
    adm = await _admit(session, qs, req, now)
    if adm.existing is not None:
        return adm.existing, False
    order = await _persist(session, adm)
    await _activate(session, qs, order, adm)
    _track_open(session, [order])
    return order, True


async def preview_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    now: datetime | None = None,
) -> CheckResult:
    """The pre-trade check for `req`, without storing anything."""
    now = now or datetime.now(UTC)
    try:
        req = _validate_request(req)
        portfolio = await session.get(Portfolio, req.portfolio_id)
        if portfolio is None:
            raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found")
        instrument = await get_instrument(session, req.symbol)
        market = market_status(instrument.market, now)
        return await _run_check(session, qs, req, portfolio, instrument, market)
    except SMTError as e:
        return CheckResult(ok=False, code=e.code, message=e.message)


async def place_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    now: datetime | None = None,
) -> Order:
    order, created = await _place(session, qs, req, now)
    if created and order.status == OrderStatus.rejected:
        raise _rejection_error(order)
    return order


_CHILD_ROLES = ("take_profit", "stop_loss")


async def _children(session: AsyncSession, parent_id: UUID) -> list[Order]:
    rows = await session.scalars(select(Order).where(Order.parent_id == parent_id))
    return sorted(rows, key=lambda o: _CHILD_ROLES.index(o.role) if o.role in _CHILD_ROLES else 9)


async def place_bracket_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    take_profit: Decimal, stop_loss: Decimal, now: datetime | None = None,
) -> list[Order]:
    """[entry, take_profit, stop_loss]; the children are `held` until the entry fills."""
    req = _validate_request(req)
    if req.type not in (OrderType.market, OrderType.limit):
        raise _invalid("a bracket entry must be a market or limit order")
    if req.qty is None:
        raise _invalid("a bracket order requires qty")
    _positive("take_profit", take_profit)
    _positive("stop_loss", stop_loss)
    instrument = await get_instrument(session, req.symbol)
    validate_price(instrument, take_profit)
    validate_price(instrument, stop_loss)
    reference = req.limit_price
    if reference is None:
        quote = await _try_quote(qs, instrument.symbol)
        if quote is not None:
            bid, ask = await _bid_ask(session, instrument, quote)
            reference = ask if req.side == OrderSide.buy else bid
    if reference is not None:
        ordered = (
            take_profit > reference > stop_loss if req.side == OrderSide.buy
            else take_profit < reference < stop_loss
        )
        if not ordered:
            raise _invalid(
                "bracket prices must satisfy take_profit > reference > stop_loss for a buy "
                "(reversed for a sell)",
                take_profit=take_profit, stop_loss=stop_loss, reference=reference,
            )

    adm = await _admit(session, qs, req, now)
    if adm.existing is not None:
        entry = adm.existing
        children = await _children(session, entry.id)
        if not children and entry.status == OrderStatus.rejected:
            return [entry]
        prices = [(c.role, c.limit_price if c.type == OrderType.limit else c.stop_price)
                  for c in children]
        if prices != [("take_profit", take_profit), ("stop_loss", stop_loss)]:
            raise _invalid(
                "client_order_id reused with different parameters",
                client_order_id=req.client_order_id, order_id=entry.id,
            )
        return [entry, *children]

    entry = await _persist(session, adm, role="entry")
    if entry.status == OrderStatus.rejected:
        raise _rejection_error(entry)
    group = uuid.uuid4()
    exit_side = OrderSide.sell if req.side == OrderSide.buy else OrderSide.buy
    children = [
        Order(
            id=uuid.uuid4(), portfolio_id=entry.portfolio_id, agent_id=entry.agent_id,
            symbol=entry.symbol, side=exit_side, type=type, tif=entry.tif, qty=entry.qty,
            limit_price=limit, stop_price=stop, filled_qty=Decimal(0),
            status=OrderStatus.held, parent_id=entry.id, oco_group_id=group, role=role,
            expires_at=entry.expires_at, source=entry.source,
        )
        for role, type, limit, stop in (
            ("take_profit", OrderType.limit, take_profit, None),
            ("stop_loss", OrderType.stop, None, stop_loss),
        )
    ]
    session.add_all(children)
    await session.flush()

    await _activate(session, qs, entry, adm)
    for child in children:
        await session.refresh(child)
    if entry.status == OrderStatus.rejected:
        await cancel_held_children(session, entry, by="system")
        raise _rejection_error(entry)
    _track_open(session, [entry, *children])
    return [entry, *children]


async def place_oco_order(
    session: AsyncSession, qs: QuoteSource, a: PlaceOrderRequest, b: PlaceOrderRequest, *,
    now: datetime | None = None,
) -> list[Order]:
    """Two resting orders in one OCO group: the fill of one cancels the other.

    Both legs are checked before anything is stored; if either is refused, both are
    stored as rejected. Canceling one leg leaves the other open.
    """
    a, b = _validate_request(a), _validate_request(b)
    if a.portfolio_id != b.portfolio_id:
        raise _invalid("OCO legs must belong to the same portfolio")
    if OrderType.market in (a.type, b.type):
        raise _invalid("OCO legs cannot be market orders")
    if a.client_order_id is not None and a.client_order_id == b.client_order_id:
        raise _invalid("OCO legs need distinct client_order_id values")
    adm_a = await _admit(session, qs, a, now)
    adm_b = await _admit(session, qs, b, now)
    if adm_a.instrument.symbol != adm_b.instrument.symbol:
        raise _invalid("OCO legs must be on the same symbol")
    if adm_a.existing is not None or adm_b.existing is not None:
        ea, eb = adm_a.existing, adm_b.existing
        if ea is None or eb is None or ea.oco_group_id is None or (
            ea.oco_group_id != eb.oco_group_id
        ):
            raise _invalid("client_order_id reused with different parameters")
        return [ea, eb]

    group = uuid.uuid4()
    oa = await _persist(session, adm_a, oco_group_id=group)
    ob = await _persist(session, adm_b, oco_group_id=group)
    failed = next((o for o in (oa, ob) if o.status == OrderStatus.rejected), None)
    if failed is not None:
        for o in (oa, ob):
            if o.status != OrderStatus.rejected:
                _reject(o, failed.reject_code, f"OCO sibling rejected: {failed.reject_message}")
        await session.flush()
        raise _rejection_error(failed)

    await _activate(session, qs, oa, adm_a)
    await session.refresh(ob)  # a fill of `a` cancels `b`
    await _activate(session, qs, ob, adm_b)
    await session.refresh(oa)
    _track_open(session, [oa, ob])
    return [oa, ob]


async def cancel_held_children(session: AsyncSession, order: Order, *, by: str) -> list[Order]:
    """Ruling R17: cancel the `held` bracket children of a rejected/expired entry.

    Locks the children FOR UPDATE (the caller already holds the portfolio lock) and
    queues one `order.canceled` event each.
    """
    children = list(
        await session.scalars(
            select(Order)
            .where(Order.parent_id == order.id, Order.status == OrderStatus.held)
            .order_by(Order.created_at, Order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if children:
        await _cancel(session, children, by)
    return children


async def _untrack_if_idle(session: AsyncSession, symbols: set[str]) -> None:
    for symbol in sorted(symbols):
        still_open = await session.scalar(
            select(Order.id)
            .where(Order.symbol == symbol, Order.status.in_(OPEN_STATUSES))
            .limit(1)
        )
        if still_open is None:
            queue_untrack(session, symbol, "orders")


async def _cancel(session: AsyncSession, orders: list[Order], by: str) -> None:
    now = datetime.now(UTC)
    for o in orders:
        o.status = OrderStatus.canceled
        o.updated_at = now
    await session.flush()
    for o in orders:
        queue_event(session, "order.canceled", {
            "order_id": o.id, "portfolio_id": o.portfolio_id, "agent_id": o.agent_id,
            "symbol": o.symbol, "by": by,
        })
    await _untrack_if_idle(session, {o.symbol for o in orders})


async def cancel_order(session: AsyncSession, order_id: UUID, *, by: str) -> Order:
    """Cancel an open order; a bracket entry takes its open children with it."""
    order = await _lock_order(session, order_id)
    if order.status not in OPEN_STATUSES:
        raise _invalid(f"cannot cancel a {order.status} order", order_id=order.id)
    children = list(
        await session.scalars(
            select(Order)
            .where(Order.parent_id == order.id, Order.status.in_(OPEN_STATUSES))
            .order_by(Order.created_at, Order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    await _cancel(session, [order, *children], by)
    return order


async def cancel_all_orders(
    session: AsyncSession, portfolio_id: UUID, symbol: str | None = None, *,
    by: str = "agent",
) -> int:
    await _lock_portfolio(session, portfolio_id)
    query = select(Order).where(
        Order.portfolio_id == portfolio_id, Order.status.in_(OPEN_STATUSES)
    )
    if symbol is not None:
        query = query.where(Order.symbol == (await get_instrument(session, symbol)).symbol)
    orders = list(
        await session.scalars(
            query.order_by(Order.created_at, Order.id).with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if orders:
        await _cancel(session, orders, by)
    return len(orders)


async def modify_order(
    session: AsyncSession, qs: QuoteSource, order_id: UUID, *,
    qty: Decimal | None = None, limit_price: Decimal | None = None,
    stop_price: Decimal | None = None, trail_pct: Decimal | None = None,
    trail_amount: Decimal | None = None, now: datetime | None = None,
) -> Order:
    """Change the parameters of a pending/queued/held order; the type cannot change.

    The risk checks run again on the new parameters (held bracket children only get the
    lot/tick validation: they close the entry's position). A refused change raises and
    leaves the order as it was. Changing a bracket entry's qty also resizes its children.
    """
    changes = {
        k: v for k, v in {
            "qty": qty, "limit_price": limit_price, "stop_price": stop_price,
            "trail_pct": trail_pct, "trail_amount": trail_amount,
        }.items() if v is not None
    }
    if not changes:
        raise _invalid("nothing to modify")
    order = await _lock_order(session, order_id)
    if order.status not in MODIFIABLE:
        raise _invalid(f"cannot modify a {order.status} order", order_id=order.id)
    if qty is not None and order.notional is not None:
        raise _invalid("qty cannot be set on a notional order", order_id=order.id)
    if order.type == OrderType.trailing_stop and (trail_pct is not None or trail_amount is not None):
        # switching between pct and amount is allowed: the new one replaces the old one
        changes.setdefault("trail_pct", None)
        changes.setdefault("trail_amount", None)
    req = PlaceOrderRequest(
        portfolio_id=order.portfolio_id, symbol=order.symbol, side=order.side,
        type=order.type, tif=order.tif,
        **{name: getattr(order, name) for name in _PARAMS},
    )
    req = _validate_request(dataclasses.replace(req, **changes))
    portfolio = await session.get(Portfolio, order.portfolio_id)
    instrument = await get_instrument(session, order.symbol)
    if order.status == OrderStatus.held:
        validate_quantity(instrument, req.qty)
        for price in (req.limit_price, req.stop_price):
            if price is not None:
                validate_price(instrument, price)
    else:
        market = market_status(instrument.market, now or datetime.now(UTC))
        check = await _run_check(session, qs, req, portfolio, instrument, market)
        if not check.ok:
            raise SMTError(check.code, check.message, {"order_id": str(order.id)})

    for name in _PARAMS:
        setattr(order, name, getattr(req, name))
    order.updated_at = datetime.now(UTC)
    if qty is not None and order.role == "entry":
        for child in await _children(session, order.id):
            if child.status in OPEN_STATUSES:
                child.qty = qty
                child.updated_at = order.updated_at
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: UUID) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        raise SMTError(ErrorCode.NOT_FOUND, "order not found", {"order_id": str(order_id)})
    return order


async def list_orders(
    session: AsyncSession, portfolio_id: UUID, *,
    status: list[OrderStatus] | None = None, limit: int = 100,
) -> list[Order]:
    """Newest first."""
    query = select(Order).where(Order.portfolio_id == portfolio_id)
    if status:
        query = query.where(Order.status.in_(status))
    query = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
    return list(await session.scalars(query))


async def _position_balances(
    session: AsyncSession, portfolio_id: UUID, symbol: str | None = None
) -> dict[str, Decimal]:
    """Non-zero ledger position balances by symbol."""
    query = (
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        )
        .order_by(Account.asset)
    )
    if symbol is not None:
        query = query.where(Account.asset == symbol)
    return {asset: balance for asset, balance in await session.execute(query)}


async def _close(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str,
    held: Decimal, qty: Decimal | None, *, source: str, agent_id: str | None,
    now: datetime | None,
) -> tuple[Order, bool]:
    size = abs(held)
    if qty is None:
        qty = size
    else:
        _positive("qty", qty)
        if qty > size:
            raise _invalid("qty exceeds the position", qty=qty, position=held)
    side = OrderSide.sell if held > 0 else OrderSide.buy
    req = PlaceOrderRequest(
        portfolio_id=portfolio_id, symbol=symbol, side=side, type=OrderType.market,
        qty=qty, source=source, agent_id=agent_id,
    )
    return await _place(session, qs, req, now)


async def close_position(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str,
    qty: Decimal | None = None, *, source: str = "agent", agent_id: str | None = None,
    now: datetime | None = None,
) -> Order:
    """Market order on the opposite side for |position| (or `qty` <= |position|)."""
    await _lock_portfolio(session, portfolio_id)
    symbol = (await get_instrument(session, symbol)).symbol
    held = (await _position_balances(session, portfolio_id, symbol)).get(symbol)
    if held is None:
        raise _invalid("no open position", symbol=symbol)
    order, created = await _close(
        session, qs, portfolio_id, symbol, held, qty, source=source, agent_id=agent_id,
        now=now,
    )
    if created and order.status == OrderStatus.rejected:
        raise _rejection_error(order)
    return order


async def close_all_positions(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, *,
    source: str = "agent", agent_id: str | None = None, now: datetime | None = None,
) -> list[Order]:
    """One closing order per open position; rejections are returned, not raised."""
    await _lock_portfolio(session, portfolio_id)
    orders = []
    for symbol, held in (await _position_balances(session, portfolio_id)).items():
        order, _ = await _close(
            session, qs, portfolio_id, symbol, held, None, source=source,
            agent_id=agent_id, now=now,
        )
        orders.append(order)
    return orders
