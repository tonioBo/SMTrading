"""Pure comparison of SMTrading's view of a broker account with the broker's (plan 21).

Items (one per thing compared, each with a `status`):

- `position:<symbol>`: the summed quantity of the link's portfolios against the broker's,
  for every symbol either side holds. Equal within QTY_TOLERANCE (fractional rounding),
  or within the broker's fee residue in the asset (`fee_dust`, Alpaca crypto): ok. A difference that the orders sent to the broker and still open can explain (ours
  within [broker − open buys, broker + open sells]): warning «ordine in corso». Anything
  else, an unknown position at the broker included: mismatch;
- `cash:<currency>`: our cash (every currency, converted to the account currency) against
  the broker's available cash. Only a shortfall counts (a dedicated account has no
  reason to hold less; a surplus is ok). Tolerance: max(0,01, 0,1 % of ours) + 1 % of the
  cash held in other currencies + what the open buys at the broker may cost (the broker
  reserves it: ok, noted). A missing FX rate: unverifiable; a missing price of an open
  buy, with a shortfall beyond the rest of the tolerance: warning;
- `order:<broker id>` (only when the broker lists its open orders): an order open here and
  sent (with a broker id) that the broker does not list, or lists with another open
  quantity or price; an order open at the broker that SMTrading does not know, or that it
  has closed. Changed within RECENT (a fill or cancel being synced) or with a cancel /
  replace in flight: warning; otherwise mismatch.

`overall` folds the items: mismatch > warning (or unverifiable items) > ok.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from smtrading.brokers.base import BrokerOpenOrder
from smtrading.common.money import decimal_to_str

QTY_TOLERANCE_ABS = Decimal("0.000001")
QTY_TOLERANCE_REL = Decimal("0.00001")
CASH_TOLERANCE_ABS = Decimal("0.01")
CASH_TOLERANCE_PCT = Decimal("0.001")
FX_BAND_PCT = Decimal("0.01")
PRICE_TOLERANCE_REL = Decimal("0.0001")
RECENT = timedelta(minutes=5)

OK, WARNING, MISMATCH, UNVERIFIABLE = "ok", "warning", "mismatch", "unverifiable"


@dataclass(frozen=True)
class OurOrder:
    """An order of a linked portfolio routed to this link, open here or with a broker id."""

    id: UUID
    portfolio_id: UUID
    symbol: str
    side: str
    type: str
    open: bool
    qty: Decimal | None  # open quantity (None: an amount order)
    limit_price: Decimal | None
    stop_price: Decimal | None
    broker_order_id: str | None
    broker_status: str | None
    sent: bool  # a send was started (broker id or broker_sent_at)
    updated_at: datetime | None


@dataclass
class CashView:
    currency: str  # the account currency
    broker: Decimal
    ours: Decimal | None  # in the account currency; None: an FX rate is missing
    foreign: Decimal = Decimal(0)  # our cash in other currencies, converted
    open_buys: Decimal | None = Decimal(0)  # None: a price is missing


@dataclass
class Comparison:
    items: list[dict] = field(default_factory=list)
    orders_checked: bool = False


def num(v: Decimal | None) -> str | None:
    return None if v is None else decimal_to_str(v)


def qty_tolerance(broker: Decimal) -> Decimal:
    return max(QTY_TOLERANCE_ABS, abs(broker) * QTY_TOLERANCE_REL)


def _same_price(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(abs(b) * PRICE_TOLERANCE_REL, Decimal("0.000001"))


def _item(key: str, kind: str, label: str, status: str, note: str, *,
          symbol: str | None = None, ours: Decimal | None = None,
          broker: Decimal | None = None, portfolio_id: UUID | None = None) -> dict:
    diff = None if ours is None or broker is None else ours - broker
    out = {"key": key, "kind": kind, "label": label, "symbol": symbol, "ours": num(ours),
           "broker": num(broker), "diff": num(diff), "status": status, "note": note}
    if portfolio_id is not None:
        out["portfolio_id"] = str(portfolio_id)
    return out


def _recent(t: datetime | None, now: datetime) -> bool:
    return t is not None and now - t < RECENT


def compare_positions(ours: dict[str, Decimal], broker: dict[str, Decimal],
                      orders: list[OurOrder],
                      fee_dust: dict[str, Decimal] | None = None) -> list[dict]:
    open_buy: dict[str, Decimal] = {}
    open_sell: dict[str, Decimal] = {}
    unbounded: set[str] = set()
    for o in orders:
        if not (o.open and o.sent):
            continue
        if o.qty is None:
            unbounded.add(o.symbol)
            continue
        side = open_buy if o.side == "buy" else open_sell
        side[o.symbol] = side.get(o.symbol, Decimal(0)) + o.qty
    items = []
    for symbol in sorted(set(ours) | set(broker)):
        mine, theirs = ours.get(symbol, Decimal(0)), broker.get(symbol, Decimal(0))
        if mine == 0 and theirs == 0:
            continue
        if abs(mine - theirs) <= max(qty_tolerance(theirs),
                                     (fee_dust or {}).get(symbol, Decimal(0))):
            status, note = OK, "uguale"
        elif symbol in unbounded or (
                theirs - open_buy.get(symbol, Decimal(0)) <= mine
                <= theirs + open_sell.get(symbol, Decimal(0))):
            status, note = WARNING, "ordine in corso: in attesa dell'aggiornamento"
        elif mine == 0:
            status, note = MISMATCH, "posizione al broker sconosciuta a SMTrading"
        elif theirs == 0:
            status, note = MISMATCH, "posizione assente al broker"
        else:
            status, note = MISMATCH, "quantità diversa"
        items.append(_item(f"position:{symbol}", "position", symbol, status, note,
                           symbol=symbol, ours=mine, broker=theirs))
    return items


def compare_cash(cash: CashView) -> dict:
    key, label = f"cash:{cash.currency}", f"Liquidità {cash.currency}"
    if cash.ours is None:
        return _item(key, "cash", label, UNVERIFIABLE, "cambio non disponibile",
                     broker=cash.broker)
    with localcontext() as ctx:
        ctx.prec = 60
        base = (max(CASH_TOLERANCE_ABS, abs(cash.ours) * CASH_TOLERANCE_PCT)
                + cash.foreign * FX_BAND_PCT)
        floor = cash.ours - base
        if cash.broker >= floor:
            status, note = OK, "uguale" if cash.broker <= cash.ours + base else \
                "più liquidità al broker"
        elif cash.open_buys is None:
            status, note = WARNING, "acquisti in corso senza prezzo: non verificabile"
        elif cash.broker >= floor - cash.open_buys:
            status, note = OK, "liquidità impegnata da acquisti in corso"
        else:
            status, note = MISMATCH, "liquidità al broker inferiore a quella di SMTrading"
    ours = cash.ours.quantize(Decimal("0.01"))
    return _item(key, "cash", label, status, note, ours=ours,
                 broker=cash.broker.quantize(Decimal("0.01")))


def _order_label(side: str, symbol: str, kind: str | None = None) -> str:
    return f"Ordine {side} {symbol}" + (f" ({kind})" if kind else "")


def compare_orders(orders: list[OurOrder], broker: list[BrokerOpenOrder],
                   now: datetime) -> list[dict]:
    by_id = {o.broker_order_id: o for o in orders if o.broker_order_id}
    theirs = {b.broker_order_id: b for b in broker}
    sending = {o.symbol for o in orders if o.open and o.sent and not o.broker_order_id}
    items = []
    for o in sorted((o for o in orders if o.open and o.broker_order_id),
                    key=lambda o: (o.symbol, o.broker_order_id)):
        b = theirs.get(o.broker_order_id)
        key = f"order:{o.broker_order_id}"
        label = _order_label(o.side, o.symbol, o.type)
        settling = _recent(o.updated_at, now) or o.broker_status in (
            "cancel_requested", "replace_requested")
        if b is None:
            items.append(_item(
                key, "order", label, WARNING if settling else MISMATCH,
                "in aggiornamento" if settling else "aperto in SMTrading, assente al broker",
                symbol=o.symbol, ours=o.qty, portfolio_id=o.portfolio_id))
            continue
        same_qty = o.qty is None or b.qty is None or abs(o.qty - b.qty) <= qty_tolerance(b.qty)
        same_limit = o.limit_price is None or _same_price(o.limit_price, b.limit_price)
        same_stop = (o.type == "trailing_stop" or o.stop_price is None
                     or _same_price(o.stop_price, b.stop_price))
        if same_qty and same_limit and same_stop:
            status, note = OK, "uguale"
        else:
            what = [w for w, same in (("quantità", same_qty), ("prezzo limite", same_limit),
                                      ("prezzo stop", same_stop)) if not same]
            status = WARNING if settling else MISMATCH
            note = ("in aggiornamento, " if settling else "") + "diverso: " + ", ".join(what)
        items.append(_item(key, "order", label, status, note, symbol=o.symbol, ours=o.qty,
                           broker=b.qty, portfolio_id=o.portfolio_id))
    for b in sorted(broker, key=lambda b: (b.symbol, b.broker_order_id)):
        mine = by_id.get(b.broker_order_id)
        if mine is not None and mine.open:
            continue
        key = f"order:{b.broker_order_id}"
        label = _order_label(b.side, b.symbol)
        if mine is not None:
            settling = _recent(mine.updated_at, now)
            items.append(_item(
                key, "order", label, WARNING if settling else MISMATCH,
                "in aggiornamento" if settling else "chiuso in SMTrading, aperto al broker",
                symbol=b.symbol, ours=Decimal(0), broker=b.qty,
                portfolio_id=mine.portfolio_id))
            continue
        settling = b.symbol in sending
        items.append(_item(
            key, "order", label, WARNING if settling else MISMATCH,
            "invio in corso" if settling else "ordine al broker sconosciuto a SMTrading",
            symbol=b.symbol, broker=b.qty))
    return items


def overall(items: list[dict]) -> str:
    statuses = {i["status"] for i in items}
    if MISMATCH in statuses:
        return MISMATCH
    if statuses & {WARNING, UNVERIFIABLE}:
        return WARNING
    return OK
