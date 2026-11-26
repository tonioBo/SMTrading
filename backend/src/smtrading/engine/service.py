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

Quotes (I2): every public call reads market data through one MemoQuoteSource,
prefetched for the portfolio and the order's symbol before anything is written;
execute_fill freezes it before its first journal, so nothing after a fill in the same
transaction (which holds the chain lock until commit) reaches the provider. `redis`,
when given, is only read: valuations fall back to cached quotes (Ruling R23).

Idempotency: a request whose client_order_id matches a stored order with the same
parameters returns that order; a stored rejection is raised again (M5).

Reduce-only (Ruling R22): closing orders and bracket exits are `reduce_only`; at fill
they are clamped to the position, or canceled when there is nothing to reduce. Closing a
position also cancels that symbol's open bracket exits in the portfolio (Ruling R16),
and its other resting orders on the closing side that are not reduce-only (a PROTEGGI
OCO, a stop or limit placed alone: `cancel_closing_side`): after a full close all of
them (once flat they would open the opposite position, and at the broker they hold the
quantity the close needs); after a partial close those whose open quantity exceeds what
is left, an OCO group as a whole (canceled rather than resized: a resize at the broker is
a cancel and a new send, and the agents re-protect the rest with a new OCO). One
`order.exits_canceled` activity says which.

Broker routing (Rulings R3, R7): in a portfolio linked to an enabled broker, an order the
adapter can take (`routable`) is not executed here: after the local checks it is stored
`pending` (`queued` for a market order on a closed market) with broker_status
`to_send`, and BrokerSync sends it once its market is open, applying the broker's fills
and final states. Canceling an order that is (or may be) at the broker only marks it
`cancel_requested`; it stays open until the broker confirms, with any last fills applied.
Modifying one marks it `replace_requested` (cancel, then send again). No network I/O
happens here, so no lock is ever held while waiting for a broker. An opening buy the
broker is known to refuse (plan 44: its cached tradability, `brokers.tradability`, says
not found, leverage only...) is rejected at once with INVALID_ORDER and that reason; the
preview answers the same.

Synthetic orders (plan 29): on a link whose broker takes only market orders (eToro,
`adapter.market_only`), a limit, stop, stop-limit or trailing order the broker would take
at market is accepted and kept here (`synthetic`, no broker_link_id): the processor
watches it like a simulated order (same quotes, stale/suspect guards, market hours,
expiry) and at the trigger hands it to BrokerSync (`hand_to_broker`), which sends it as a
market order. Its OCO siblings are only suspended meanwhile (they protect the position,
never trigger): canceled once it fills, resized to what is left after a partial fill,
active again if it ends unfilled (refused, failed, expired). The bracket exits of such a
portfolio are synthetic too and stay here once active. The order keeps its own type and
prices; the fill is the broker's. A synthetic sell beyond the position (a short) is
refused at placement with the broker's text (an OCO as a whole, before either leg is
stored); at the trigger and again at the send a sell is clamped to the position less the
portfolio's sells already at the broker (eToro closes FIFO on the whole account). Once
handed over the order cannot be modified any more. Alpaca crypto keeps its resting
orders the same way (`adapter.synthetic`: stops, trailing stops, every resting sell; a buy
limit / stop limit goes to Alpaca), its bracket exits and OCO legs included; so do Alpaca
equities with every leg of an OCO group, bracket exits included (plan 47: Alpaca holds the
shares for the first resting sell and refuses a second one; a stop placed alone still
rests at Alpaca). A sell beyond
the long position by no more than the broker's fee residue in the asset (`fee_dust`:
Alpaca keeps its crypto buy fee in the coin, so a sell of the quantity ordered on the
entry exceeds the position) is sized to the position at placement (`_fee_sized`).

Market closed: market orders are `queued` (the processor executes them at the open);
other types rest as `pending` and are only evaluated once the market is open, because a
queued order is executed at market. While the market is closed the quote age is not
checked at placement; it is checked again at execution.

Valid until (plan 16, good-till-time): `valid_until` is an aware ISO datetime or
"close" (today's session close of the instrument's market, early and manual closes
applied; refused for CRYPTO and FX, and when there is no session left today). It must be
after the placement; a malformed one is INVALID_ORDER with `field: valid_until`, nothing
stored. It applies to the order itself (a bracket's entry, never its exits: they keep
their own tif): `expires_at` becomes the earlier of it and the day expiry, so the
processor expires an unfilled order locally (never sent to a broker included). An order
at the broker is canceled by the processor through the normal cancel path when
`valid_until` passes (`cancel_requested`), and BrokerSync records the confirmed cancel
as `expired`. A replay of a client_order_id is looked up before "close" is resolved; a
reuse with valid_until given or left out differently is refused.
"""

import dataclasses
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import ActivityLevel
from smtrading.activity.service import record_activity
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import open_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.execution import broker_live, execute_fill, hand_to_broker, plan_refusal
from smtrading.engine.models import (
    BROKER_LIQUIDATION_REASON,
    FILLABLE_STATUSES,
    LIQUIDATION_ROLE,
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
from smtrading.engine.triggers import OrderView, TriggerAction, evaluate
from smtrading.events import POST_COMMIT_KEY, notify, queue_event, queue_track, queue_untrack
from smtrading.instruments.market_hours import (
    MarketStatus,
    local_date,
    market_status,
    session_on,
)
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import get_instrument, validate_price, validate_quantity
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_live_portfolio, lock_portfolio
from smtrading.portfolios.models import (
    Portfolio,
    PortfolioStatus,
    ensure_not_archived,
    live_portfolio_ids,
)
from smtrading.risk.checks import CheckResult, pre_trade_check
from smtrading.risk.daily_loss import check_daily_loss_safely
from smtrading.risk.settings import get_default_spread_bps, set_kill_switch

log = logging.getLogger("smtrading.engine.service")

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
    reduce_only: bool = False
    # intraday: a buy meant to be closed the same session (set by the portal/internal code,
    # never by the agents' tools)
    day_trade: bool = False
    # R8.2: a reduce-only market order of "Chiudi tutte le posizioni reali" (passes the kill switch)
    live_flatten: bool = False
    # plan 16: an aware datetime (normalized to UTC), "close", or None (no end)
    valid_until: datetime | str | None = None
    # mode B: the automatic rule an order belongs to (set by the scalper, never by tools)
    auto_rule_id: UUID | None = None


VALID_UNTIL_CLOSE = "close"
_NO_CLOSE_MARKETS = ("CRYPTO", "FX")  # no daily session close: "close" is refused
_VALID_UNTIL_FORMAT = (
    'valid_until must be an ISO 8601 datetime with a timezone offset or "close"')


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
    return dataclasses.replace(req, valid_until=_parse_valid_until(req.valid_until))


def _bad_valid_until(message: str, value) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message,
                    {"field": "valid_until", "valid_until": str(value)})


def _parse_valid_until(value) -> datetime | str | None:
    """None, VALID_UNTIL_CLOSE, or an aware datetime in UTC (INVALID_ORDER otherwise)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == VALID_UNTIL_CLOSE:
            return VALID_UNTIL_CLOSE
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            raise _bad_valid_until(_VALID_UNTIL_FORMAT, value) from None
    if not isinstance(value, datetime):
        raise _bad_valid_until(_VALID_UNTIL_FORMAT, value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise _bad_valid_until(
            "valid_until needs a timezone offset (e.g. 2026-11-18T21:55:00+01:00)", value)
    return value.astimezone(UTC)


def _resolve_valid_until(value: datetime | str | None, instrument: Instrument,
                         now: datetime) -> datetime | None:
    """The end of a parsed valid_until: "close" is today's session close of the
    instrument's market (on its local date). It must be after `now`."""
    if value is None:
        return None
    if value == VALID_UNTIL_CLOSE:
        if instrument.market in _NO_CLOSE_MARKETS:
            raise _bad_valid_until(
                f'"close" is not available for {instrument.market}: give a datetime', value)
        session = session_on(instrument.market, local_date(instrument.market, now))
        if session is None:
            raise _bad_valid_until(
                f"{instrument.market} has no session today: give a datetime", value)
        value = session[1]
    assert isinstance(value, datetime)
    if value <= now:
        raise _bad_valid_until(
            "valid_until must be in the future (the market may already be closed today)",
            value)
    return value


async def _memo(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbols: list[str]
) -> MemoQuoteSource:
    """The call's memo, prefetched for the portfolio and `symbols` (I2). Run after the
    portfolio lock and before any write."""
    memo = MemoQuoteSource.wrap(qs)
    await prefetch_portfolio(session, memo, portfolio_id, symbols=symbols)
    return memo


async def _lock_order(session: AsyncSession, order_id: UUID) -> Order:
    """Portfolio row first, then the order row (Ruling R11)."""
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        raise SMTError(ErrorCode.NOT_FOUND, "order not found", {"order_id": str(order_id)})
    await lock_portfolio(session, portfolio_id)
    return (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _try_quote(qs: QuoteSource, symbol: str) -> Quote | None:
    """The quote to act on at placement; None when there is none or the quality guard
    marked it suspect (no immediate fill or trigger on it: the processor waits)."""
    try:
        quote = (await qs.get_quotes([symbol])).get(symbol)
    except SMTError:
        return None
    return None if quote is not None and quote.suspect is not None else quote


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


def _same_valid_until(order: Order, value: datetime | str | None) -> bool:
    """"close" matches any stored end (it resolved on the day of the first placement)."""
    if value == VALID_UNTIL_CLOSE:
        return order.valid_until is not None
    return order.valid_until == value


def _same_params(order: Order, req: PlaceOrderRequest, symbol: str) -> bool:
    return (
        order.symbol == symbol and order.side == req.side and order.type == req.type
        and order.tif == req.tif and order.reduce_only == req.reduce_only
        and order.day_trade == req.day_trade and order.auto_rule_id == req.auto_rule_id
        and all(getattr(order, name) == getattr(req, name) for name in _PARAMS)
        and _same_valid_until(order, req.valid_until)
    )


@dataclass
class _Admission:
    req: PlaceOrderRequest
    portfolio: Portfolio
    instrument: Instrument
    market: MarketStatus
    now: datetime
    qs: MemoQuoteSource
    redis: object = None
    existing: Order | None = None
    check: CheckResult | None = None


async def _run_check(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, portfolio: Portfolio,
    instrument: Instrument, market: MarketStatus, redis=None, *, now: datetime | None = None,
    live_effects: bool = True, exclude_order: UUID | None = None,
) -> CheckResult:
    """`live_effects=False` (preview, modify): the real-money fuse never trips the
    portfolio; `exclude_order` (modify) is not counted by it."""
    return await pre_trade_check(
        session, qs, portfolio=portfolio, instrument=instrument, side=req.side, qty=req.qty,
        notional=req.notional, order_type=req.type, limit_price=req.limit_price,
        stop_price=req.stop_price, check_quote_age=market.is_open,
        reduce_only=req.reduce_only, redis=redis, live_flatten=req.live_flatten, now=now,
        live_effects=live_effects, exclude_order=exclude_order, day_trade=req.day_trade,
        auto_rule_id=req.auto_rule_id,
    )


async def _admit(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, now: datetime | None,
    redis=None, *, check: bool = True,
) -> _Admission:
    """Validated request + locked portfolio + idempotency lookup + risk check (no writes).

    With `check=False` the risk check is left to the caller (`_check_admission`)."""
    now = now or datetime.now(UTC)
    req = _validate_request(req)
    portfolio = await lock_live_portfolio(session, req.portfolio_id)
    instrument = await get_instrument(session, req.symbol)
    market = market_status(instrument.market, now)
    memo = await _memo(session, qs, portfolio.id, [instrument.symbol])
    sized = await _fee_sized(session, portfolio, instrument, req)
    adm = _Admission(req, portfolio, instrument, market, now, memo, redis)
    if req.client_order_id is not None:
        # under the portfolio lock: a concurrent duplicate waits and then sees this row
        existing = await session.scalar(
            select(Order).where(
                Order.portfolio_id == portfolio.id,
                Order.client_order_id == req.client_order_id,
            )
        )
        if existing is not None:
            if not (_same_params(existing, req, instrument.symbol)
                    or _same_params(existing, sized, instrument.symbol)):
                raise _invalid(
                    "client_order_id reused with different parameters",
                    client_order_id=req.client_order_id, order_id=existing.id,
                )
            adm.existing = existing
            return adm
    adm.req = dataclasses.replace(
        sized, valid_until=_resolve_valid_until(req.valid_until, instrument, now))
    if check:
        await _check_admission(session, adm)
    return adm


async def _fee_sized(session: AsyncSession, portfolio: Portfolio, instrument: Instrument,
                     req: PlaceOrderRequest) -> PlaceOrderRequest:
    """A sell beyond the long position by no more than the linked broker's fee residue in
    the asset (Alpaca crypto keeps its buy fee in the coin: a sell of the quantity
    ordered on the entry) is the position: sized to it, never a short."""
    if (req.side != OrderSide.sell or req.qty is None or portfolio.broker_link_id is None):
        return req
    held = await session.scalar(
        select(AccountBalance.balance)
        .join(Account, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == portfolio.id, Account.asset == instrument.symbol,
               Account.kind == AccountKind.position)
    ) or Decimal(0)
    if held <= 0 or req.qty <= held:
        return req
    link = await session.get(BrokerLink, portfolio.broker_link_id)
    if link is None or not link.enabled:
        return req
    async with open_adapter(session, link.id, None) as adapter:  # config only
        dust = adapter.fee_dust(instrument.symbol, held)
    return dataclasses.replace(req, qty=held) if req.qty - held <= dust else req


async def _check_admission(session: AsyncSession, adm: _Admission) -> None:
    adm.check = await _run_check(
        session, adm.qs, adm.req, adm.portfolio, adm.instrument, adm.market, adm.redis,
        now=adm.now,
    )


def _reject(order: Order, code: ErrorCode | str, message: str | None) -> None:
    order.status = OrderStatus.rejected
    order.reject_code = str(code)
    order.reject_message = (message or "")[:512]
    order.updated_at = datetime.now(UTC)


def _tif_expiry(adm: _Admission) -> datetime | None:
    """The end of a DAY order (None for GTC), valid_until left out."""
    if adm.req.tif != TimeInForce.day:
        return None
    return _day_expiry(adm.instrument, adm.portfolio, adm.market, adm.now)


async def _persist(session: AsyncSession, adm: _Admission, **extra) -> Order:
    req, check = adm.req, adm.check
    order = Order(
        id=uuid.uuid4(), portfolio_id=req.portfolio_id, agent_id=req.agent_id,
        client_order_id=req.client_order_id, symbol=adm.instrument.symbol, side=req.side,
        type=req.type, tif=req.tif, qty=req.qty, notional=req.notional,
        limit_price=req.limit_price, stop_price=req.stop_price, trail_pct=req.trail_pct,
        trail_amount=req.trail_amount, filled_qty=Decimal(0), status=OrderStatus.new,
        source=req.source, reduce_only=req.reduce_only, day_trade=req.day_trade,
        live_flatten=req.live_flatten, valid_until=req.valid_until,
        auto_rule_id=req.auto_rule_id, **extra,
    )
    if check is not None and not check.ok:
        _reject(order, check.code, check.message)
        if check.details:
            order.reject_details = check.details  # not stored: the raised error carries it
    else:
        ends = [t for t in (_tif_expiry(adm), req.valid_until) if t is not None]
        order.expires_at = min(ends) if ends else None
    session.add(order)
    await session.flush()
    return order


def _rejection_error(order: Order) -> SMTError:
    try:
        code = ErrorCode(order.reject_code)
    except ValueError:
        code = ErrorCode.INVALID_ORDER
    details = {**(getattr(order, "reject_details", None) or {}), "order_id": str(order.id)}
    return SMTError(code, order.reject_message or "order rejected", details, persisted=True)


def _raise_if_rejected(*orders: Order) -> None:
    """A new or replayed (M5) rejection is raised with its stored code."""
    for order in orders:
        if order.status == OrderStatus.rejected:
            raise _rejection_error(order)


async def _fill_now(
    session: AsyncSession, qs: QuoteSource, order: Order, quote: Quote, redis=None
) -> None:
    effects = session.info.setdefault(POST_COMMIT_KEY, [])
    mark = len(effects)
    try:
        async with session.begin_nested():
            await execute_fill(session, qs, order.id, quote=quote, redis=redis)
    except SMTError as e:
        del session.info.setdefault(POST_COMMIT_KEY, [])[mark:]
        await session.refresh(order)
        _reject(order, e.code, e.message)
        await session.flush()


async def _hand_now(session: AsyncSession, order: Order, link_id) -> None:
    """Triggered at placement on a linked portfolio: to the broker after the fill-time
    checks; a failed check rejects the order, like a failed simulated fill."""
    try:
        async with session.begin_nested():
            await hand_to_broker(session, order, link_id)
    except SMTError as e:
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
    bid: Decimal, ask: Decimal, redis=None,
) -> None:
    # at most two steps: a stop_limit first triggers, then may fill as a limit
    for _ in range(2):
        result = evaluate(_view(order), bid, ask)
        if result.new_trail_ref is not None:
            order.trail_ref_price = result.new_trail_ref
        if result.action in (TriggerAction.fill, TriggerAction.trigger_to_market):
            link_id = (await session.get(Portfolio, order.portfolio_id)).broker_link_id
            if link_id is not None:  # Ruling R10 (a)/R11
                await _hand_now(session, order, link_id)
                return
        if result.action == TriggerAction.fill:
            await session.flush()
            await _fill_now(session, qs, order, quote, redis)
            return
        if result.action == TriggerAction.trigger_to_market:
            order.status = OrderStatus.triggered
            await session.flush()
            await _fill_now(session, qs, order, quote, redis)
            return
        if result.action != TriggerAction.trigger_to_limit:
            return
        order.status = OrderStatus.triggered


async def _activate(session: AsyncSession, qs: QuoteSource, order: Order, adm: _Admission) -> None:
    """Move a freshly stored `new` order to queued/pending, executing it when it can.

    After an immediate fill the daily loss limit is checked (best-effort)."""
    await _activate_order(session, qs, order, adm)
    if order.status == OrderStatus.filled:
        await check_daily_loss_safely(
            session, qs, order.portfolio_id, adm.now, redis=adm.redis
        )


UNSUPPORTED_BY_BROKER = "Ordine non supportato dal broker collegato"


async def _sells_beyond_position(session: AsyncSession, order: Order) -> bool:
    """A sell that is not reduce-only and exceeds the long position: it would open a short
    (plan 29: refused at placement on a link that cannot short)."""
    if order.side != OrderSide.sell or order.reduce_only or order.qty is None:
        return False
    held = await session.scalar(
        select(AccountBalance.balance)
        .join(Account, AccountBalance.account_id == Account.id)
        .where(Account.portfolio_id == order.portfolio_id, Account.asset == order.symbol,
               Account.kind == AccountKind.position)
    ) or Decimal(0)
    return order.qty > held


async def _keeps_exits_local(session: AsyncSession, portfolio: Portfolio,
                             instrument: Instrument, exits: list[Order]) -> bool:
    """Plan 29: the bracket exits of a portfolio linked to a market-only broker are
    synthetic (kept and watched here once active, sent at market at the trigger)."""
    if portfolio.broker_link_id is None:
        return False
    link = await session.get(BrokerLink, portfolio.broker_link_id)
    if link is None or not link.enabled:
        return False
    async with open_adapter(session, link.id, None) as adapter:  # config only
        return all(adapter.synthetic(o, instrument) for o in exits)


async def _oco_short_leg(session: AsyncSession, adm: _Admission) -> str | None:
    """Plan 29 (F): the broker's refusal text when this OCO leg would be a synthetic sell
    beyond the position (refused by `_route`), checked before any leg is stored."""
    if adm.check is not None and not adm.check.ok:
        return None
    link_id = adm.portfolio.broker_link_id
    link = await session.get(BrokerLink, link_id) if link_id is not None else None
    if link is None or not link.enabled:
        return None
    req = adm.req
    leg = Order(  # transient, never added to the session
        portfolio_id=req.portfolio_id, symbol=adm.instrument.symbol, side=req.side,
        type=req.type, tif=req.tif, qty=req.qty, notional=req.notional,
        limit_price=req.limit_price, stop_price=req.stop_price, trail_pct=req.trail_pct,
        trail_amount=req.trail_amount, filled_qty=Decimal(0), reduce_only=req.reduce_only,
    )
    async with open_adapter(session, link_id, None) as adapter:  # config only
        synthetic = (not adapter.routable(leg, adm.instrument)
                     and adapter.synthetic(leg, adm.instrument))
        beyond = adapter.SELL_BEYOND_POSITION_MESSAGE
    if synthetic and beyond is not None and await _sells_beyond_position(session, leg):
        return beyond
    return None


async def _broker_refusal(session: AsyncSession, redis, portfolio: Portfolio,
                          instrument: Instrument, side: OrderSide, reduce_only: bool
                          ) -> str | None:
    """Plan 44: why the broker of the linked portfolio would refuse this opening buy,
    from its cached tradability (Redis only, no network); None: nothing known against."""
    if side != OrderSide.buy or reduce_only or portfolio.broker_link_id is None:
        return None
    from smtrading.brokers.tradability import cached_refusal

    return await cached_refusal(session, redis, portfolio, instrument)


async def _route(session: AsyncSession, order: Order, adm: _Admission) -> bool:
    """True when the order was handed to the portfolio's broker, or refused because the
    broker cannot take it (Ruling R10: nothing fills locally on a linked portfolio; a
    trailing stop the broker lacks is triggered here and then sent as a market order)."""
    link_id = adm.portfolio.broker_link_id
    if link_id is None:
        return False
    link = await session.get(BrokerLink, link_id)
    if link is None or not link.enabled:
        _reject(order, ErrorCode.PROVIDER_UNAVAILABLE, "Collegamento al broker disattivato")
        return True
    refusal = await _broker_refusal(session, adm.redis, adm.portfolio, adm.instrument,
                                    order.side, order.reduce_only)
    if refusal is not None:  # plan 44: the broker is known to refuse it
        _reject(order, ErrorCode.INVALID_ORDER, refusal)
        return True
    async with open_adapter(session, link_id, None) as adapter:  # no network: config only
        routable = adapter.routable(order, adm.instrument)
        reason = None if routable else adapter.unsupported_reason(order, adm.instrument)
        synthetic = not routable and adapter.synthetic(order, adm.instrument)
        beyond = adapter.SELL_BEYOND_POSITION_MESSAGE
    if synthetic:  # plan 29: kept and watched here, sent at market at the trigger
        if beyond is not None and await _sells_beyond_position(session, order):
            _reject(order, ErrorCode.SHORT_NOT_ALLOWED, beyond)
            return True
        order.synthetic = True
        return False
    if not routable:
        if order.type == OrderType.trailing_stop:
            return False  # simulated trigger, see hand_to_broker
        _reject(order, ErrorCode.INVALID_ORDER, reason or UNSUPPORTED_BY_BROKER)
        return True
    order.broker_link_id = link_id
    order.broker_status = BrokerState.TO_SEND
    order.status = (
        OrderStatus.queued
        if order.type == OrderType.market and not adm.market.is_open
        else OrderStatus.pending
    )
    order.updated_at = datetime.now(UTC)
    return True


async def _activate_order(
    session: AsyncSession, qs: QuoteSource, order: Order, adm: _Admission
) -> None:
    if order.status != OrderStatus.new:
        return
    if await _route(session, order, adm):
        await session.flush()
        return
    instrument = adm.instrument
    quote = await _try_quote(qs, instrument.symbol)
    if order.type == OrderType.market:
        if not adm.market.is_open:
            order.status = OrderStatus.queued
        elif quote is None:
            _reject(order, ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")
        else:
            await _fill_now(session, qs, order, quote, adm.redis)
        await session.flush()
        return
    order.status = OrderStatus.pending
    if quote is not None:
        bid, ask = await _bid_ask(session, instrument, quote)
        if order.type == OrderType.trailing_stop:
            order.trail_ref_price = bid if order.side == OrderSide.sell else ask
        if adm.market.is_open:
            await _evaluate_now(session, qs, order, quote, bid, ask, adm.redis)
    await session.flush()


def _track_open(session: AsyncSession, orders: list[Order]) -> None:
    if any(o.status in OPEN_STATUSES for o in orders):
        queue_track(session, orders[0].symbol, "orders")


async def _place(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, now: datetime | None,
    redis=None,
) -> tuple[Order, bool]:
    """(order, created); a stored rejection is returned, not raised."""
    adm = await _admit(session, qs, req, now, redis)
    if adm.existing is not None:
        return adm.existing, False
    order = await _persist(session, adm)
    await _activate(session, adm.qs, order, adm)
    _track_open(session, [order])
    # with the final outcome of the placement (a failed fill rejects it after _persist)
    queue_event(session, "order.rejected" if order.status == OrderStatus.rejected
                else "order.accepted", {
                    "order_id": order.id, "portfolio_id": order.portfolio_id,
                    "agent_id": order.agent_id, "symbol": order.symbol,
                })
    return order, True


async def preview_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    now: datetime | None = None, redis=None,
) -> CheckResult:
    """The pre-trade check for `req`, without storing anything."""
    now = now or datetime.now(UTC)
    try:
        req = _validate_request(req)
        portfolio = await session.get(Portfolio, req.portfolio_id)
        if portfolio is None:
            raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found")
        ensure_not_archived(portfolio)
        instrument = await get_instrument(session, req.symbol)
        _resolve_valid_until(req.valid_until, instrument, now)
        market = market_status(instrument.market, now)
        refusal = await _broker_refusal(session, redis, portfolio, instrument, req.side,
                                        req.reduce_only)
        if refusal is not None:
            return CheckResult(ok=False, code=ErrorCode.INVALID_ORDER, message=refusal)
        return await _run_check(session, qs, req, portfolio, instrument, market, redis,
                                now=now, live_effects=False)
    except SMTError as e:
        return CheckResult(ok=False, code=e.code, message=e.message)


async def place_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    now: datetime | None = None, redis=None,
) -> Order:
    order, _ = await _place(session, qs, req, now, redis)
    _raise_if_rejected(order)
    return order


_CHILD_ROLES = ("take_profit", "stop_loss")


async def _children(session: AsyncSession, parent_id: UUID) -> list[Order]:
    rows = await session.scalars(select(Order).where(Order.parent_id == parent_id))
    return sorted(rows, key=lambda o: _CHILD_ROLES.index(o.role) if o.role in _CHILD_ROLES else 9)


def _check_bracket_prices(
    side: OrderSide, take_profit: Decimal | None, stop_loss: Decimal | None,
    reference: Decimal | None,
) -> None:
    """take_profit > reference > stop_loss for a buy entry (reversed for a sell); a
    missing (None) price is left out of the comparison."""
    chain = [v for v in (take_profit, reference, stop_loss) if v is not None]
    if side == OrderSide.sell:
        chain.reverse()
    if not all(hi > lo for hi, lo in pairwise(chain)):
        raise _invalid(
            "bracket prices must satisfy take_profit > reference > stop_loss for a buy "
            "(reversed for a sell)",
            take_profit=take_profit, stop_loss=stop_loss, reference=reference,
        )


async def place_bracket_order(
    session: AsyncSession, qs: QuoteSource, req: PlaceOrderRequest, *,
    take_profit: Decimal, stop_loss: Decimal, now: datetime | None = None, redis=None,
) -> list[Order]:
    """[entry, take_profit, stop_loss]; the children are `held` until the entry fills.

    The children are reduce_only (Ruling R22). A replayed client_order_id is looked up
    before the prices are validated against the current quote."""
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

    adm = await _admit(session, qs, req, now, redis, check=False)
    if adm.existing is not None:
        entry = adm.existing
        children = await _children(session, entry.id)
        _raise_if_rejected(entry)
        prices = [(c.role, c.limit_price if c.type == OrderType.limit else c.stop_price)
                  for c in children]
        if prices != [("take_profit", take_profit), ("stop_loss", stop_loss)]:
            raise _invalid(
                "client_order_id reused with different parameters",
                client_order_id=req.client_order_id, order_id=entry.id,
            )
        return [entry, *children]

    reference = req.limit_price
    if reference is None:
        quote = await _try_quote(adm.qs, instrument.symbol)
        if quote is not None:
            bid, ask = await _bid_ask(session, instrument, quote)
            reference = ask if req.side == OrderSide.buy else bid
    _check_bracket_prices(req.side, take_profit, stop_loss, reference)
    await _check_admission(session, adm)

    entry = await _persist(session, adm, role="entry")
    _raise_if_rejected(entry)
    group = uuid.uuid4()
    exit_side = OrderSide.sell if req.side == OrderSide.buy else OrderSide.buy
    children = [
        Order(
            id=uuid.uuid4(), portfolio_id=entry.portfolio_id, agent_id=entry.agent_id,
            symbol=entry.symbol, side=exit_side, type=type, tif=entry.tif, qty=entry.qty,
            limit_price=limit, stop_price=stop, filled_qty=Decimal(0),
            status=OrderStatus.held, parent_id=entry.id, oco_group_id=group, role=role,
            # the exits keep their own tif: the entry's valid_until is not theirs
            expires_at=_tif_expiry(adm), source=entry.source, reduce_only=True,
        )
        for role, type, limit, stop in (
            ("take_profit", OrderType.limit, take_profit, None),
            ("stop_loss", OrderType.stop, None, stop_loss),
        )
    ]
    if await _keeps_exits_local(session, adm.portfolio, instrument, children):
        for child in children:
            child.synthetic = True
    session.add_all(children)
    await session.flush()

    await _activate(session, adm.qs, entry, adm)
    for child in children:
        await session.refresh(child)
    if entry.status == OrderStatus.rejected:
        await cancel_held_children(session, entry, by="system")
        raise _rejection_error(entry)
    _track_open(session, [entry, *children])
    return [entry, *children]


async def place_oco_order(
    session: AsyncSession, qs: QuoteSource, a: PlaceOrderRequest, b: PlaceOrderRequest, *,
    now: datetime | None = None, redis=None,
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
    adm_a = await _admit(session, qs, a, now, redis)
    adm_b = await _admit(session, adm_a.qs, b, now, redis)
    if adm_a.instrument.symbol != adm_b.instrument.symbol:
        raise _invalid("OCO legs must be on the same symbol")
    if adm_a.existing is not None or adm_b.existing is not None:
        ea, eb = adm_a.existing, adm_b.existing
        if ea is None or eb is None or ea.oco_group_id is None or (
            ea.oco_group_id != eb.oco_group_id
        ):
            raise _invalid("client_order_id reused with different parameters")
        _raise_if_rejected(ea, eb)
        return [ea, eb]

    for adm in (adm_a, adm_b):  # plan 29 (F): refused as a whole, never one leg alone
        beyond = await _oco_short_leg(session, adm)
        if beyond is not None:
            adm.check = CheckResult(ok=False, code=ErrorCode.SHORT_NOT_ALLOWED, message=beyond)
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

    await _activate(session, adm_a.qs, oa, adm_a)
    await session.refresh(ob)  # a fill of `a` cancels `b`
    await _activate(session, adm_b.qs, ob, adm_b)
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


async def order_closed(session: AsyncSession, order: Order) -> None:
    """After a broker-managed order became final: untrack its symbol when idle; unlock
    a portfolio whose broker liquidation is over (Ruling R10 b)."""
    await session.flush()
    await _untrack_if_idle(session, {order.symbol})
    if order.role != LIQUIDATION_ROLE:
        return
    still = await session.scalar(
        select(Order.id).where(
            Order.portfolio_id == order.portfolio_id, Order.role == LIQUIDATION_ROLE,
            Order.status.in_(OPEN_STATUSES), Order.id != order.id,
        ).limit(1)
    )
    portfolio = await session.get(Portfolio, order.portfolio_id)
    if (
        still is None and portfolio.status == PortfolioStatus.locked
        and portfolio.reduce_only_reason == BROKER_LIQUIDATION_REASON
    ):
        portfolio.status = PortfolioStatus.active
        portfolio.reduce_only_reason = None
        log.warning("portfolio %s unlocked: broker liquidation done", portfolio.id)


async def reject_broker_order(session: AsyncSession, order: Order, code: ErrorCode | str,
                              message: str | None) -> None:
    """The one finalisation of a broker-managed order rejected locally or by the broker
    (BrokerSync, the real-money disarm): rejected with `code`/`message`, its held bracket
    children canceled (Ruling R17), `order.rejected` notified, then `order_closed`. The
    caller holds the portfolio and order locks (in that order)."""
    order.status = OrderStatus.rejected
    order.reject_code = str(code)
    # a NUL from the broker would make the order UPDATE fail (Postgres text)
    order.reject_message = (message or "").replace("\x00", "")[:512]
    order.updated_at = datetime.now(UTC)
    await cancel_held_children(session, order, by="system")
    await notify(
        session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
        kind="order.rejected",
        payload={
            "order_id": order.id, "symbol": order.symbol, "code": str(code),
            "message": message, "broker": True,
        },
    )
    await order_closed(session, order)


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
    """Cancel locally, or ask BrokerSync to cancel at the broker (the order stays open
    until the broker confirms, Ruling R3)."""
    now = datetime.now(UTC)
    for o in orders:
        o.updated_at = now
        if broker_live(o):
            o.broker_status = BrokerState.CANCEL_REQUESTED
        else:
            o.status = OrderStatus.canceled
    await session.flush()
    for o in orders:
        queue_event(
            session,
            "order.canceled" if o.status == OrderStatus.canceled else "order.cancel_requested",
            {
                "order_id": o.id, "portfolio_id": o.portfolio_id, "agent_id": o.agent_id,
                "symbol": o.symbol, "by": by,
            },
        )
    await _untrack_if_idle(session, {o.symbol for o in orders})


async def cancel_orders(session: AsyncSession, orders: list[Order], *, by: str) -> None:
    """Cancel these open orders, already locked FOR UPDATE by the caller (with their
    portfolio): locally, or cancel_requested for BrokerSync when at the broker."""
    if orders:
        await _cancel(session, orders, by)


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
    await lock_live_portfolio(session, portfolio_id)
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


async def set_kill_switch_and_cancel(
    session: AsyncSession, on: bool, *, cancel_open: bool, by: str = "admin"
) -> int:
    """Turn the kill switch on/off; with `on` and `cancel_open`, cancel every open order
    (portfolio by portfolio in id order, each portfolio row locked first and its open
    orders read again under the lock, so a placement that committed meanwhile is
    caught; M2). Returns how many. Fills opening exposure are refused at execution
    while the switch is on (I3)."""
    await set_kill_switch(session, on)
    canceled = 0
    if on and cancel_open:
        portfolio_ids = list(await session.scalars(
            live_portfolio_ids().order_by(Portfolio.id)  # archived: no open orders
        ))
        for portfolio_id in portfolio_ids:
            canceled += await cancel_all_orders(session, portfolio_id, by=by)
    queue_event(session, "kill_switch", {"on": bool(on), "canceled": canceled})
    return canceled


async def modify_order(
    session: AsyncSession, qs: QuoteSource, order_id: UUID, *,
    qty: Decimal | None = None, limit_price: Decimal | None = None,
    stop_price: Decimal | None = None, trail_pct: Decimal | None = None,
    trail_amount: Decimal | None = None, now: datetime | None = None, redis=None,
) -> Order:
    """Change the parameters of a pending/queued/held order; the type cannot change.

    The risk checks run again on the new parameters (held bracket children only get the
    lot/tick validation and the take_profit > entry limit > stop_loss ordering against
    the entry and the sibling: they close the entry's position). A refused change raises and
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
    ensure_not_archived(await session.get(Portfolio, order.portfolio_id))
    if order.status not in MODIFIABLE:
        raise _invalid(f"cannot modify a {order.status} order", order_id=order.id)
    if order.synthetic and order.broker_link_id is not None:
        # plan 29: triggered, being sent at market: a change would be ignored
        raise _invalid(f"cannot modify a {order.status} order: it is being executed",
                       order_id=order.id)
    replace = broker_live(order)
    if replace and (
        order.broker_status in BrokerState.PENDING_WORK or order.filled_qty > 0
    ):
        raise SMTError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "a broker operation on this order is in progress; retry later",
            {"order_id": str(order.id), "broker_status": str(order.broker_status)},
        )
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
        await _check_held_child_prices(session, order, req)
    else:
        market = market_status(instrument.market, now or datetime.now(UTC))
        check = await _run_check(
            session, MemoQuoteSource.wrap(qs), req, portfolio, instrument, market, redis,
            now=now, live_effects=False, exclude_order=order.id,
        )
        if not check.ok:
            raise SMTError(check.code, check.message, {"order_id": str(order.id)})

    for name in _PARAMS:
        setattr(order, name, getattr(req, name))
    order.updated_at = datetime.now(UTC)
    if replace:  # BrokerSync cancels the broker order and sends the new parameters
        order.broker_status = BrokerState.REPLACE_REQUESTED
    if qty is not None and order.role == "entry":
        for child in await _children(session, order.id):
            if child.status in OPEN_STATUSES:
                child.qty = qty
                child.updated_at = order.updated_at
    await session.flush()
    return order


async def _check_held_child_prices(
    session: AsyncSession, order: Order, req: PlaceOrderRequest
) -> None:
    """A held bracket exit keeps take_profit > entry limit > stop_loss (buy entry)."""
    if order.role not in _CHILD_ROLES or order.parent_id is None:
        return
    entry = await session.get(Order, order.parent_id)
    prices = {
        c.role: c.limit_price if c.role == "take_profit" else c.stop_price
        for c in await _children(session, order.parent_id)
        if c.id != order.id and c.status in OPEN_STATUSES
    }
    prices[order.role] = req.limit_price if order.role == "take_profit" else req.stop_price
    reference = (
        entry.limit_price if entry is not None and entry.type == OrderType.limit else None
    )
    entry_side = OrderSide.buy if order.side == OrderSide.sell else OrderSide.sell
    _check_bracket_prices(
        entry_side, prices.get("take_profit"), prices.get("stop_loss"), reference
    )


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


async def _cancel_exits(
    session: AsyncSession, portfolio_id: UUID, symbol: str, *, by: str
) -> list[Order]:
    """Ruling R16: cancel the symbol's open bracket exits (take_profit/stop_loss). A
    `held` exit whose entry is not filled belongs to a still-pending bracket and is kept."""
    entry = aliased(Order)
    exits = list(await session.scalars(
        select(Order)
        .outerjoin(entry, entry.id == Order.parent_id)
        .where(
            Order.portfolio_id == portfolio_id, Order.symbol == symbol,
            Order.role.in_(_CHILD_ROLES), Order.status.in_(OPEN_STATUSES),
            (Order.status != OrderStatus.held) | (entry.status == OrderStatus.filled),
        )
        .order_by(Order.created_at, Order.id)
        .with_for_update(of=Order)
        .execution_options(populate_existing=True)
    ))
    if exits:
        await _cancel(session, exits, by)
    return exits


async def _cancel_with_held_children(session: AsyncSession, orders: list[Order],
                                    by: str) -> None:
    """Cancel these locked open orders; a bracket entry canceled here (not at the broker)
    takes its held exits with it. One canceled at the broker leaves them to BrokerSync's
    final state (a partly filled entry keeps them for the filled quantity)."""
    await _cancel(session, orders, by)
    for o in orders:
        if o.status == OrderStatus.canceled:
            await cancel_held_children(session, o, by=by)


async def cancel_closing_side(
    session: AsyncSession, portfolio_id: UUID, symbol: str, side: OrderSide, *,
    remaining: Decimal, exclude: UUID | None, by: str, agent_id: str | None = None,
) -> list[Order]:
    """With a close on `side` (module docstring): cancel the symbol's open resting orders
    on that side that are not reduce-only, other than `exclude` (the close) and those
    already being canceled: all of them when `remaining` (what the close leaves) is 0,
    else those whose open quantity exceeds it (an OCO group as a whole). The caller holds
    the portfolio lock. Returns the canceled orders."""
    candidates = [o for o in await session.scalars(
        select(Order).where(
            Order.portfolio_id == portfolio_id, Order.symbol == symbol,
            Order.side == side, Order.status.in_(FILLABLE_STATUSES),
            Order.reduce_only.is_not(True),
        ).order_by(Order.created_at, Order.id)
        .with_for_update().execution_options(populate_existing=True)
    ) if o.id != exclude and o.broker_status != BrokerState.CANCEL_REQUESTED]

    def exceeds(o: Order) -> bool:
        return o.qty is None or o.qty - (o.filled_qty or Decimal(0)) > remaining

    groups = {o.oco_group_id for o in candidates if exceeds(o) and o.oco_group_id}
    orders = [o for o in candidates
              if remaining <= 0 or exceeds(o) or o.oco_group_id in groups]
    if not orders:
        return []
    await _cancel_with_held_children(session, orders, by)
    if remaining <= 0:
        summary = (f"Chiusura di {symbol}: annullati anche {len(orders)} ordini di uscita "
                   "non reduce-only (a posizione chiusa aprirebbero quella opposta)")
    else:
        summary = (f"Chiusura parziale di {symbol}: annullati {len(orders)} ordini di "
                   f"uscita con quantità oltre la posizione restante "
                   f"({decimal_to_str(remaining)}): riproteggi la quantità restante")
    record_activity(session, ActivityDraft(
        "order.exits_canceled", ActivityLevel.info, False, summary,
        {"symbol": symbol, "order_ids": [o.id for o in orders],
         "remaining": decimal_to_str(remaining),
         "by": by}),
        portfolio_id=portfolio_id, agent_id=agent_id)
    return orders


async def cancel_plan_additions(session: AsyncSession, orders: list[Order], *,
                                by: str = "system") -> list[Order]:
    """Of these open orders (locked, with their portfolio), cancel those that would add
    to a position of an ended «piano» still being closed, or turn it (goals.plans,
    `plan_refusal`): one `plan.orders_canceled` activity per symbol. Returns them."""
    blocked = [o for o in orders
               if o.status in FILLABLE_STATUSES
               and o.broker_status != BrokerState.CANCEL_REQUESTED
               and await plan_refusal(session, o) is not None]
    if not blocked:
        return []
    await _cancel_with_held_children(session, blocked, by)
    by_symbol: dict[tuple[UUID, str], list[Order]] = {}
    for o in blocked:
        by_symbol.setdefault((o.portfolio_id, o.symbol), []).append(o)
    for (portfolio_id, symbol), group in by_symbol.items():
        what = ("annullato 1 ordine che aumenterebbe" if len(group) == 1
                else f"annullati {len(group)} ordini che aumenterebbero")
        record_activity(session, ActivityDraft(
            "plan.orders_canceled", ActivityLevel.info, False,
            f"{symbol}: {what} la posizione in chiusura a fine piano "
            "(nessuna aggiunta né inversione)",
            {"symbol": symbol, "order_ids": [o.id for o in group], "reason": "plan_grace",
             "by": by}),
            portfolio_id=portfolio_id, agent_id=group[0].agent_id)
    return blocked


async def _close(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str,
    held: Decimal, qty: Decimal | None, *, source: str, agent_id: str | None,
    now: datetime | None, redis=None, limit_price: Decimal | None = None,
    client_order_id: str | None = None,
) -> tuple[Order, bool]:
    """A reduce_only market order against `held` (Ruling R22), or with `limit_price` a
    reduce_only DAY limit (it expires at the session end, never rests overnight); unless
    it is rejected, the symbol's open bracket exits are canceled (Ruling R16), and its
    resting orders on the closing side that are not reduce-only (`cancel_closing_side`)."""
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
        qty=qty, source=source, agent_id=agent_id, reduce_only=True,
        client_order_id=client_order_id,
    )
    if limit_price is not None:
        req = dataclasses.replace(req, type=OrderType.limit, limit_price=limit_price,
                                  tif=TimeInForce.day)
    order, created = await _place(session, qs, req, now, redis)
    if order.status != OrderStatus.rejected:
        await _cancel_exits(session, portfolio_id, symbol, by=source)
        await cancel_closing_side(session, portfolio_id, symbol, side, remaining=size - qty,
                                  exclude=order.id, by=source, agent_id=agent_id)
    return order, created


async def close_position(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, symbol: str,
    qty: Decimal | None = None, *, source: str = "agent", agent_id: str | None = None,
    now: datetime | None = None, redis=None, limit_price: Decimal | None = None,
    client_order_id: str | None = None,
) -> Order:
    """Reduce-only market order on the opposite side for |position| (or `qty` <=
    |position|); with `limit_price` a reduce-only DAY limit at that price instead;
    `client_order_id` makes it idempotent. The symbol's open bracket exits are canceled
    (Ruling R16), and its standalone exits that the close leaves oversized
    (`cancel_closing_side`)."""
    await lock_live_portfolio(session, portfolio_id)
    symbol = (await get_instrument(session, symbol)).symbol
    held = (await _position_balances(session, portfolio_id, symbol)).get(symbol)
    if held is None:
        raise _invalid("no open position", symbol=symbol)
    order, _ = await _close(
        session, qs, portfolio_id, symbol, held, qty, source=source, agent_id=agent_id,
        now=now, redis=redis, limit_price=limit_price, client_order_id=client_order_id,
    )
    _raise_if_rejected(order)
    return order


async def close_all_positions(
    session: AsyncSession, qs: QuoteSource, portfolio_id: UUID, *,
    source: str = "agent", agent_id: str | None = None, now: datetime | None = None,
    redis=None,
) -> list[Order]:
    """One closing order per open position; rejections are returned, not raised.

    One memo, prefetched for every held symbol before the first close (I2)."""
    await lock_live_portfolio(session, portfolio_id)
    positions = await _position_balances(session, portfolio_id)
    qs = await _memo(session, qs, portfolio_id, list(positions))
    orders = []
    for symbol, held in positions.items():
        order, _ = await _close(
            session, qs, portfolio_id, symbol, held, None, source=source,
            agent_id=agent_id, now=now, redis=redis,
        )
        orders.append(order)
    return orders
