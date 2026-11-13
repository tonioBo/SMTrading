"""Pure execution-price helpers (no DB, no IO)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide, OrderType

_PREC = 60
_BPS = Decimal(10000)


@dataclass(frozen=True)
class QuoteView:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    ts: datetime


def effective_bid_ask(q: QuoteView, default_spread_bps: int) -> tuple[Decimal, Decimal]:
    """Bid/ask, synthesising a missing side.

    - from `last`: last -+ last * bps / 2 / 10000;
    - without `last`, or when the side synthesised from `last` would cross (or touch)
      the side that is present: the present side -+ present * bps / 10000 (full spread).
    A synthesised side never crosses the present one.
    """
    with localcontext() as ctx:
        ctx.prec = _PREC
        bid, ask, last = q.bid, q.ask, q.last
        if bid is not None and ask is not None:
            return bid, ask
        frac = Decimal(default_spread_bps) / _BPS
        half = last * frac / 2 if last is not None else None
        if bid is None and ask is None:
            if last is None:
                raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "quote has no usable price")
            return last - half, last + half
        if bid is None:
            if last is None or last - half >= ask:
                return ask * (1 - frac), ask
            return last - half, ask
        if last is None or last + half <= bid:
            return bid, bid * (1 + frac)
        return bid, last + half


def market_price(
    side: OrderSide, q: QuoteView, *, slippage_bps: int, default_spread_bps: int
) -> Decimal:
    bid, ask = effective_bid_ask(q, default_spread_bps)
    with localcontext() as ctx:
        ctx.prec = _PREC
        slip = Decimal(slippage_bps) / _BPS
        if side == OrderSide.buy:
            return ask * (1 + slip)
        return bid * (1 - slip)


def round_to_tick(price: Decimal, tick: Decimal, side: OrderSide) -> Decimal:
    """Round to a tick multiple: up for buys, down for sells."""
    mode = ROUND_UP if side == OrderSide.buy else ROUND_DOWN
    with localcontext() as ctx:
        ctx.prec = _PREC
        steps = (price / tick).quantize(Decimal(1), rounding=mode)
        return steps * tick


def fill_price(
    order_type: OrderType,
    side: OrderSide,
    q: QuoteView,
    *,
    limit_price: Decimal | None,
    slippage_bps: int,
    default_spread_bps: int,
    tick: Decimal,
) -> Decimal:
    raw = market_price(side, q, slippage_bps=slippage_bps, default_spread_bps=default_spread_bps)
    price = round_to_tick(raw, tick, side)
    if order_type in (OrderType.limit, OrderType.stop_limit):
        if limit_price is None:
            raise SMTError(ErrorCode.INVALID_ORDER, "limit price required")
        # never worse than the limit (clamp after rounding)
        price = min(price, limit_price) if side == OrderSide.buy else max(price, limit_price)
    return price


def notional_to_qty(notional: Decimal, price: Decimal, lot: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PREC
        lots = (notional / price / lot).quantize(Decimal(1), rounding=ROUND_DOWN)
        qty = lots * lot
    if qty <= 0:
        raise SMTError(ErrorCode.INVALID_ORDER, "notional too small for one lot")
    return qty
