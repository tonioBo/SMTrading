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
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

_T212 = "/api/v0/equity"
_T212_INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "MSFT_US_EQ", "shortName": "MSFT", "type": "STOCK", "currencyCode": "USD"},
]
_ETORO_INSTRUMENTS = {
    "AAPL": 1001,
    "MSFT": 1002,
    "BTC": 100000,
}
_ETORO_SEARCH = "/api/v1/market-data/search"
_ETORO_RATES = "/api/v1/market-data/instruments/rates"
_ETORO_CANDLES = re.compile(
    r"^/api/v1/market-data/instruments/(\d+)/history/candles/(asc|desc)/(\w+)/(\d+)$")
_ETORO_INTERVALS = {
    "OneMinute": timedelta(minutes=1), "FiveMinutes": timedelta(minutes=5),
    "TenMinutes": timedelta(minutes=10), "FifteenMinutes": timedelta(minutes=15),
    "ThirtyMinutes": timedelta(minutes=30), "OneHour": timedelta(hours=1),
    "FourHours": timedelta(hours=4), "OneDay": timedelta(days=1), "OneWeek": timedelta(weeks=1),
}
_ETORO_OPEN = re.compile(r"^/api/v2/trading/execution/(demo/)?orders$")
_ETORO_CANCEL = re.compile(r"^/api/v2/trading/execution/(demo/)?orders/(\d+)$")
_ETORO_LOOKUP = re.compile(r"^/api/v2/trading/info/(demo/)?orders:lookup$")
_ETORO_CLOSE = re.compile(r"^/api/v1/trading/execution/(demo/)?market-close-orders/positions/(\d+)$")
_ETORO_CLOSE_STATUS = re.compile(r"^/api/v1/trading/info/(demo|real)/close-orders/(\d+)$")
_ETORO_PNL = re.compile(r"^/api/v1/trading/info/(demo|real)/pnl$")
_ETORO_ELIGIBILITY = re.compile(r"^/api/v2/trading/info/(demo/)?eligibility$")
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


@dataclass
class _EtoroEnv:
    credit: Decimal = Decimal("1000.00")
    positions: dict[int, dict] = field(default_factory=dict)  # positionID -> position
    orders: dict[int, dict] = field(default_factory=dict)  # orderId -> lookup record
    refs: dict[str, int] = field(default_factory=dict)  # referenceId -> orderId
    closes: dict[int, dict] = field(default_factory=dict)  # close orderID -> status


class FakeLiveBroker:
    def __init__(self, price: Decimal = Decimal(100)) -> None:
        self.price = price
        self.alpaca = _Account("USD")
        self.t212 = _Account("EUR")
        self.etoro = {"demo": _EtoroEnv(), "real": _EtoroEnv()}
        self.etoro_partial_close = True  # allowPartialClosePosition of every instrument
        self.etoro_fail_close_after: int | None = None  # closes accepted before a 500
        # RateLimit-Remaining of the market-data answers (None: no quota headers)
        self.market_data_remaining: int | None = None
        self.calls: deque[tuple[str, str]] = deque(maxlen=1000)  # the latest requests
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
            self.calls.append((method, path))
            if path.startswith(("/api/v1/", "/api/v2/")):
                return self._etoro(request, method, path, body)
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

    # --- eToro ---------------------------------------------------------------------

    def _etoro(self, request: httpx.Request, method: str, path: str,
               body: dict) -> httpx.Response:
        if not request.headers.get("x-api-key") or not request.headers.get("x-user-key"):
            return httpx.Response(401, json={"errorMessage": "Unauthorized"})
        params = request.url.params
        if path.startswith("/api/v1/market-data/"):
            resp = self._etoro_market_data(method, path, params)
            if self.market_data_remaining is not None:
                resp.headers.update({
                    "RateLimit-Limit": "60", "RateLimit-Policy": "60;w=60", "RateLimit-Reset": "60",
                    "RateLimit-Remaining": str(self.market_data_remaining)})
            return resp
        env = self.etoro["demo" if "/demo/" in path else "real"]
        if method == "POST" and _ETORO_OPEN.match(path):
            return self._etoro_open(env, body, request.headers.get("x-request-id") or "")
        if method == "POST" and _ETORO_ELIGIBILITY.match(path):
            ids = body.get("instrumentIds") or []
            return httpx.Response(200, json={"instruments": [
                {"instrumentId": iid, "allowOpenPosition": True,
                 "allowPartialClosePosition": self.etoro_partial_close,
                 "leverageConfigs": [{"settlementType": "real", "direction": "buy",
                                      "leverageValues": [1]}]}
                for iid in ids if iid in _ETORO_INSTRUMENTS.values()]})
        if method == "GET" and _ETORO_LOOKUP.match(path):
            oid = params.get("orderId")
            if oid is None and params.get("referenceId") in env.refs:
                oid = env.refs[params["referenceId"]]
            order = env.orders.get(int(oid)) if str(oid or "").isdigit() else None
            return _numbers(200, order) if order else httpx.Response(
                404, json={"errorMessage": "Order not found"})
        m = _ETORO_CANCEL.match(path)
        if method == "DELETE" and m:
            if int(m.group(2)) not in env.orders:
                return httpx.Response(404, json={"errorMessage": "Order not found"})
            return httpx.Response(400, json={"errorMessage": "Order cannot be cancelled"})
        m = _ETORO_CLOSE.match(path)
        if method == "POST" and m:
            return self._etoro_close(env, int(m.group(2)), body)
        m = _ETORO_CLOSE_STATUS.match(path)
        if method == "GET" and m:
            close = env.closes.get(int(m.group(2)))
            return _numbers(200, close) if close else httpx.Response(
                404, json={"errorMessage": "Order not found"})
        if method == "GET" and _ETORO_PNL.match(path):
            return _numbers(200, {"clientPortfolio": {
                "credit": env.credit, "accountCurrencyId": 1,
                "positions": list(env.positions.values()), "orders": [],
                "ordersForOpen": [], "ordersForClose": [], "mirrors": []}})
        return httpx.Response(404, json={"errorMessage": "Not found"})

    def _etoro_market_data(self, method: str, path: str, params) -> httpx.Response:
        """search, rates and candles: the same answers on both environments (market
        data has no environment in the path), prices in USD (`conversionRate*` 1)."""
        if method != "GET":
            return httpx.Response(405, json={"errorMessage": "Method not allowed"})
        if path == _ETORO_SEARCH:
            query = str(params.get("internalSymbolFull") or "").upper()
            items = [{"instrumentId": iid, "internalSymbolFull": sym,
                      "isCurrentlyTradable": True, "isBuyEnabled": True, "isDelisted": False}
                     for sym, iid in _ETORO_INSTRUMENTS.items() if query and query in sym]
            return httpx.Response(200, json={"items": items})
        known = set(_ETORO_INSTRUMENTS.values())
        if path == _ETORO_RATES:
            ids = [int(x) for x in str(params.get("instrumentIds") or "").split(",")
                   if x.strip().isdigit()]
            spread = self.price / 1000
            return _numbers(200, {"rates": [
                {"instrumentID": iid, "bid": self.price - spread, "ask": self.price + spread,
                 "lastExecution": self.price, "conversionRateBid": Decimal(1),
                 "conversionRateAsk": Decimal(1), "date": _now(), "priceRateID": next(self._ids)}
                for iid in ids if iid in known]})
        m = _ETORO_CANDLES.match(path)
        if m and int(m.group(1)) in known and m.group(3) in _ETORO_INTERVALS:
            iid, step, count = int(m.group(1)), _ETORO_INTERVALS[m.group(3)], int(m.group(4))
            if not 1 <= count <= 1000:
                return httpx.Response(400, json={"errorMessage": "Invalid candlesCount"})
            now = datetime.now(UTC)
            last = now - (now - datetime(1970, 1, 1, tzinfo=UTC)) % step  # the forming one
            candles = [
                {"instrumentID": iid,
                 "fromDate": (last - step * i).isoformat().replace("+00:00", "Z"),
                 "open": self.price, "high": self.price, "low": self.price,
                 "close": self.price, "volume": Decimal(10)}
                for i in range(count)]
            if m.group(2) == "asc":
                candles.reverse()
            return _numbers(200, {"interval": m.group(3), "candles": [
                {"instrumentId": iid, "candles": candles}]})
        return httpx.Response(404, json={"errorMessage": "Not found"})

    def _etoro_open(self, env: _EtoroEnv, body: dict, ref: str) -> httpx.Response:
        if ref in env.refs:  # x-request-id is the idempotency key
            oid = env.refs[ref]
            return httpx.Response(200, json={"token": "t", "orderId": oid, "referenceId": ref})
        iid = body.get("instrumentId")
        if (body.get("action") != "open" or body.get("transaction") != "buy"
                or body.get("orderType") != "mkt" or body.get("leverage") != 1
                or body.get("settlementType") != "real" or "sellShort" in body
                or iid not in _ETORO_INSTRUMENTS.values()):
            return httpx.Response(400, json={"errorMessage": "Invalid order"})
        try:
            if body.get("units") is not None:
                units = Decimal(str(body["units"]))
            else:
                units = Decimal(str(body.get("amount"))) / self.price
        except ArithmeticError:
            return httpx.Response(400, json={"errorMessage": "Invalid units"})
        cost = units * self.price
        if units <= 0 or cost > env.credit:
            return httpx.Response(400, json={"errorMessage": "Insufficient funds"})
        env.credit -= cost
        oid, pid = next(self._ids), next(self._ids)
        env.positions[pid] = {
            "positionID": pid, "instrumentID": iid, "isBuy": True, "units": units,
            "openRate": self.price, "amount": cost, "leverage": 1, "mirrorID": 0,
            "settlementTypeID": 1, "totalFees": Decimal(0), "openDateTime": _now()}
        env.orders[oid] = {
            "orderId": oid, "referenceId": ref,
            "status": {"id": 3, "name": "Filled", "errorCode": None, "errorMessage": None},
            "asset": {"instrumentId": iid, "settlementType": "real", "leverage": 1,
                      "side": "buy"},
            "positionExecutions": [{"positionId": pid, "openingData": {
                "units": units, "avgPrice": self.price, "fees": Decimal(0)}}],
            "totalCosts": Decimal(0)}
        env.refs[ref] = oid
        return httpx.Response(200, json={"token": "t", "orderId": oid, "referenceId": ref})

    def _etoro_close(self, env: _EtoroEnv, pid: int, body: dict) -> httpx.Response:
        if self.etoro_fail_close_after is not None:
            if self.etoro_fail_close_after <= 0:
                return httpx.Response(500, json={"errorMessage": "Internal error"})
            self.etoro_fail_close_after -= 1
        pos = env.positions.get(pid)
        if pos is None or body.get("InstrumentID") != pos["instrumentID"]:
            return httpx.Response(404, json={"errorMessage": "Position not found"})
        raw = body.get("UnitsToDeduct")
        units = pos["units"] if raw is None else Decimal(str(raw))
        if raw is not None and not self.etoro_partial_close:
            return httpx.Response(400, json={"errorMessage": "Partial close not allowed"})
        if units <= 0 or units > pos["units"]:
            return httpx.Response(400, json={"errorMessage": "Invalid units"})
        pos["units"] -= units
        if pos["units"] == 0:
            del env.positions[pid]
        env.credit += units * self.price
        cid = next(self._ids)
        env.closes[cid] = {"statusID": 3, "unitsDeducted": units, "closeRate": self.price}
        return _numbers(200, {"orderForClose": {
            "positionID": pid, "instrumentID": pos["instrumentID"], "unitsToDeduct": raw,
            "orderID": cid, "statusID": 1}, "token": "t"})


_shared: FakeLiveBroker | None = None
_shared_lock = threading.Lock()


def fake_broker() -> FakeLiveBroker:
    """The process-wide fake (every LiveClient of the process sees the same account)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = FakeLiveBroker()
        return _shared
