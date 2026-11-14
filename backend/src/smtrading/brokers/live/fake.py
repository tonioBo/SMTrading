"""FakeLiveBroker: an in-memory stand-in for the live brokers, for tests and e2e only.

`make_live_client` uses its transport instead of the real HTTP transport only when the
`live_fake_broker` setting is on (off by default): the live gate still checks every
request in front of it. It answers the account, positions and orders endpoints of both
APIs (Alpaca `/v2/...`, Trading 212 `/api/v0/equity/...`), dispatching on the path; the
host is never looked at, nothing is ever resolved or connected.

State: an empty account with 1000.00 of cash (USD for Alpaca, EUR for Trading 212).
Market orders fill at once at `price` (cash and positions updated, a buy beyond the cash
is rejected); other orders stay open until canceled.
"""

import itertools
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import httpx

_T212 = "/api/v0/equity"
_T212_INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "MSFT_US_EQ", "shortName": "MSFT", "type": "STOCK", "currencyCode": "USD"},
]
_ALPACA_ORDER = re.compile(r"^/v2/orders/([^/]+)$")
_T212_ORDER = re.compile(rf"^{_T212}/orders/(\d+)$")
_T212_PLACE = re.compile(rf"^{_T212}/orders/(market|limit|stop|stop_limit)$")


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f")


def _mark(v: object) -> object:
    if isinstance(v, Decimal):
        return f"\x00{_num(v)}\x00"
    if isinstance(v, dict):
        return {k: _mark(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_mark(x) for x in v]
    return v


def _numbers(status: int, data: object) -> httpx.Response:
    """A JSON response with Decimals written as numeric literals (as Trading 212 sends)."""
    text = json.dumps(_mark(data)).replace('"\\u0000', "").replace('\\u0000"', "")
    return httpx.Response(status, content=text,
                          headers={"content-type": "application/json"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _Account:
    currency: str
    cash: Decimal = Decimal("1000.00")
    positions: dict[str, Decimal] = field(default_factory=dict)
    orders: dict[str, dict] = field(default_factory=dict)


class FakeLiveBroker:
    def __init__(self, price: Decimal = Decimal(100)) -> None:
        self.price = price
        self.alpaca = _Account("USD")
        self.t212 = _Account("EUR")
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- dispatch ------------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method.upper()
        body: dict = {}
        if request.content:
            try:
                body = json.loads(request.content, parse_float=Decimal)
            except ValueError:
                return httpx.Response(400, json={"message": "invalid JSON"})
        with self._lock:
            if path.startswith(_T212 + "/"):
                return self._t212(method, path, body)
            if path.startswith("/v2/"):
                return self._alpaca(method, path, request.url.params, body)
        return httpx.Response(404, json={"message": "not found"})

    def _fill(self, acct: _Account, symbol: str, qty: Decimal) -> bool:
        """Signed qty (negative = sell). False if a buy exceeds the cash."""
        cost = qty * self.price
        if cost > acct.cash:
            return False
        acct.cash -= cost
        held = acct.positions.get(symbol, Decimal(0)) + qty
        if held == 0:
            acct.positions.pop(symbol, None)
        else:
            acct.positions[symbol] = held
        return True

    # --- Alpaca --------------------------------------------------------------------

    def _alpaca(self, method: str, path: str, params, body: dict) -> httpx.Response:
        acct = self.alpaca
        if (method, path) == ("GET", "/v2/account"):
            cash = _num(acct.cash)
            return httpx.Response(200, json={
                "id": "fake-alpaca-account", "status": "ACTIVE", "currency": acct.currency,
                "cash": cash, "buying_power": cash, "multiplier": "1",
                "crypto_status": "INACTIVE"})
        if (method, path) == ("GET", "/v2/positions"):
            return httpx.Response(200, json=[
                {"symbol": s, "qty": _num(q), "side": "long" if q > 0 else "short",
                 "asset_class": "us_equity"} for s, q in acct.positions.items()])
        if (method, path) == ("GET", "/v2/orders"):
            orders = list(acct.orders.values())
            if params.get("status") == "open":
                orders = [o for o in orders if o["status"] == "new"]
            return httpx.Response(200, json=orders)
        if (method, path) == ("GET", "/v2/orders:by_client_order_id"):
            ref = params.get("client_order_id")
            found = next((o for o in acct.orders.values() if o["client_order_id"] == ref),
                         None)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if (method, path) == ("POST", "/v2/orders"):
            return self._alpaca_place(body)
        m = _ALPACA_ORDER.match(path)
        if m and m.group(1) in acct.orders:
            o = acct.orders[m.group(1)]
            if method == "GET":
                return httpx.Response(200, json=o)
            if method == "DELETE":
                if o["status"] != "new":
                    return httpx.Response(422, json={"message": "order is not cancelable"})
                o["status"] = "canceled"
                return httpx.Response(204)
        return httpx.Response(404, json={"message": "not found"})

    def _alpaca_place(self, body: dict) -> httpx.Response:
        acct = self.alpaca
        if any(o["client_order_id"] == body.get("client_order_id")
               for o in acct.orders.values()):
            return httpx.Response(422, json={"message": "client_order_id must be unique"})
        try:
            qty = Decimal(str(body.get("qty") or 0))
            notional = body.get("notional")
            if notional is not None:
                qty = Decimal(str(notional)) / self.price
        except ArithmeticError:
            return httpx.Response(422, json={"message": "invalid qty"})
        if qty <= 0 or body.get("side") not in ("buy", "sell"):
            return httpx.Response(422, json={"message": "invalid order"})
        signed = qty if body["side"] == "buy" else -qty
        order = {"id": f"fake-{next(self._ids)}", "client_order_id": body.get("client_order_id"),
                 "symbol": body.get("symbol"), "side": body["side"], "type": body.get("type"),
                 "qty": _num(qty), "status": "new", "filled_qty": "0",
                 "filled_avg_price": None, "created_at": _now()}
        if body.get("type") == "market":
            if not self._fill(acct, str(body.get("symbol")), signed):
                return httpx.Response(403, json={"message": "insufficient buying power"})
            order.update(status="filled", filled_qty=_num(qty),
                         filled_avg_price=_num(self.price))
        acct.orders[order["id"]] = order
        return httpx.Response(200, json=order)

    # --- Trading 212 ---------------------------------------------------------------

    def _t212(self, method: str, path: str, body: dict) -> httpx.Response:
        acct = self.t212
        if method == "GET" and path == f"{_T212}/metadata/instruments":
            return httpx.Response(200, json=_T212_INSTRUMENTS)
        if method == "GET" and path == f"{_T212}/account/summary":
            return _numbers(200, {
                "id": 1000001, "currency": acct.currency,
                "cash": {"availableToTrade": acct.cash, "inPies": 0, "reservedForOrders": 0}})
        if method == "GET" and path == f"{_T212}/positions":
            return _numbers(200, [{"instrument": {"ticker": t}, "quantity": q}
                                  for t, q in acct.positions.items()])
        if method == "GET" and path == f"{_T212}/orders":
            return _numbers(200, [o for o in acct.orders.values() if o["status"] == "NEW"])
        if method == "GET" and path == f"{_T212}/history/orders":
            items = [{"order": o, "fill": {"price": self.price}}
                     for o in acct.orders.values() if o["status"] != "NEW"]
            return _numbers(200, {"items": items, "nextPagePath": None})
        m = _T212_PLACE.match(path)
        if method == "POST" and m:
            return self._t212_place(m.group(1), body)
        m = _T212_ORDER.match(path)
        if m and m.group(1) in acct.orders:
            o = acct.orders[m.group(1)]
            if method == "GET":  # only pending orders are served, like the real API
                return _numbers(200, o) if o["status"] == "NEW" else httpx.Response(404)
            if method == "DELETE":
                if o["status"] != "NEW":
                    return httpx.Response(400, json={"code": "NotCancelable"})
                o["status"] = "CANCELLED"
                return httpx.Response(200)
        return httpx.Response(404, json={"code": "NotFound"})

    def _t212_place(self, kind: str, body: dict) -> httpx.Response:
        acct = self.t212
        try:
            qty = Decimal(str(body.get("quantity")))
        except ArithmeticError:
            return httpx.Response(400, json={"code": "InvalidQuantity"})
        ticker = str(body.get("ticker") or "")
        if qty == 0 or not ticker:
            return httpx.Response(400, json={"code": "InvalidOrder"})
        oid = str(next(self._ids))
        order = {"id": int(oid), "ticker": ticker, "quantity": qty,
                 "type": kind.upper(), "status": "NEW", "filledQuantity": 0,
                 "createdAt": _now()}
        if kind == "market":
            if not self._fill(acct, ticker, qty):
                return httpx.Response(400, json={"code": "InsufficientFreeForStocksBuy"})
            order.update(status="FILLED", filledQuantity=qty)
        acct.orders[oid] = order
        return _numbers(200, order)


_shared: FakeLiveBroker | None = None
_shared_lock = threading.Lock()


def fake_broker() -> FakeLiveBroker:
    """The process-wide fake (every LiveClient of the process sees the same account)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = FakeLiveBroker()
        return _shared
