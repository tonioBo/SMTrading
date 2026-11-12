"""Pure trigger evaluation for resting orders (no DB, no IO)."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from smtrading.engine.models import OrderSide, OrderStatus, OrderType

_PREC = 60
_HUNDRED = Decimal(100)


class TriggerAction(StrEnum):
    none = "none"
    fill = "fill"
    trigger_to_market = "trigger_to_market"
    trigger_to_limit = "trigger_to_limit"
    update_trail = "update_trail"


@dataclass(frozen=True)
class OrderView:
    type: OrderType
    side: OrderSide
    status: OrderStatus
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_pct: Decimal | None = None
    trail_amount: Decimal | None = None
    trail_ref_price: Decimal | None = None


@dataclass(frozen=True)
class TriggerResult:
    action: TriggerAction
    new_trail_ref: Decimal | None = None


_NONE = TriggerResult(TriggerAction.none)
_MARKET_STATUSES = (OrderStatus.queued, OrderStatus.new, OrderStatus.pending)


def _limit_hit(o: OrderView, bid: Decimal, ask: Decimal) -> bool:
    if o.limit_price is None:
        return False
    return ask <= o.limit_price if o.side == OrderSide.buy else bid >= o.limit_price


def _stop_hit(o: OrderView, bid: Decimal, ask: Decimal) -> bool:
    if o.stop_price is None:
        return False
    return ask >= o.stop_price if o.side == OrderSide.buy else bid <= o.stop_price


def _trailing(o: OrderView, bid: Decimal, ask: Decimal) -> TriggerResult:
    sell = o.side == OrderSide.sell
    with localcontext() as ctx:
        ctx.prec = _PREC
        ref = o.trail_ref_price
        if ref is None:
            new_ref = bid if sell else ask
        else:
            new_ref = max(ref, bid) if sell else min(ref, ask)
        if o.trail_pct is not None:
            factor = o.trail_pct / _HUNDRED
            stop = new_ref * (1 - factor) if sell else new_ref * (1 + factor)
        elif o.trail_amount is not None:
            stop = new_ref - o.trail_amount if sell else new_ref + o.trail_amount
        else:
            return _NONE
        fired = bid <= stop if sell else ask >= stop
    if fired:
        return TriggerResult(TriggerAction.trigger_to_market, new_ref)
    if new_ref != ref:
        return TriggerResult(TriggerAction.update_trail, new_ref)
    return _NONE


def evaluate(o: OrderView, bid: Decimal, ask: Decimal) -> TriggerResult:
    if o.type == OrderType.market:
        return TriggerResult(TriggerAction.fill) if o.status in _MARKET_STATUSES else _NONE
    if o.type == OrderType.limit:
        if o.status == OrderStatus.pending and _limit_hit(o, bid, ask):
            return TriggerResult(TriggerAction.fill)
        return _NONE
    if o.type == OrderType.stop:
        if o.status == OrderStatus.pending and _stop_hit(o, bid, ask):
            return TriggerResult(TriggerAction.trigger_to_market)
        return _NONE
    if o.type == OrderType.stop_limit:
        if o.status == OrderStatus.pending and _stop_hit(o, bid, ask):
            return TriggerResult(TriggerAction.trigger_to_limit)
        if o.status == OrderStatus.triggered and _limit_hit(o, bid, ask):
            return TriggerResult(TriggerAction.fill)
        return _NONE
    if o.type == OrderType.trailing_stop and o.status == OrderStatus.pending:
        return _trailing(o, bid, ask)
    return _NONE
