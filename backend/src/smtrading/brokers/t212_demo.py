import asyncio
import base64
import json
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx

from smtrading.brokers.base import BrokerAdapter, BrokerOrder, BrokerStatus
from smtrading.brokers.registry import register
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

DEMO_URL = "https://demo.trading212.com"
_API = "/api/v0/equity"
_CACHE_TTL = 24 * 3600
_REFRESH_BACKOFF_S = 60  # the metadata endpoint allows 1 request / 50s
_HISTORY_PAGES = 5
_MATCH_SKEW = timedelta(seconds=60)  # clock skew allowed when matching createdAt

# Official docs (https://docs.trading212.com/api, OpenAPI bundle): order statuses.
_STATUS: dict[str, BrokerStatus] = {
    "LOCAL": "accepted",
    "UNCONFIRMED": "accepted",
    "CONFIRMED": "accepted",
    "NEW": "accepted",
    "CANCELLING": "accepted",
    "REPLACING": "accepted",
    "PARTIALLY_FILLED": "partially_filled",
    "FILLED": "filled",
    "CANCELLED": "canceled",
    "REPLACED": "canceled",
    "REJECTED": "rejected",
}
_ENDPOINT = {
    OrderType.market: "market",
    OrderType.limit: "limit",
    OrderType.stop: "stop",
    OrderType.stop_limit: "stop_limit",
}
_REJECT_HTTP = {400, 422}

# In-process instrument metadata cache (the endpoint allows 1 request / 50s).
_cache: dict[str, object] = {"at": 0.0, "items": None, "failed_at": None}
_cache_lock = asyncio.Lock()
# ticker -> canonical symbol, learned from resolve_ticker (positions use it)
_reverse: dict[str, str] = {}


def _dec(v: object) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f")


def _body(fields: dict) -> str:
    """JSON with Decimals written as raw numeric literals (no float rounding)."""
    marked = {k: f"\x00{_num(v)}\x00" if isinstance(v, Decimal) else v for k, v in fields.items()}
    text = json.dumps(marked)
    return text.replace('"\\u0000', "").replace('\\u0000"', "")


def _jsonable(v: object) -> object:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return "error"
    if isinstance(body, dict):
        msg = body.get("message") or body.get("clarification") or body.get("code")
        if msg:
            return str(msg)
    return "error"


def _items(cache: object) -> list[dict]:
    return cache if isinstance(cache, list) else []


def resolve_ticker(symbol: str, asset_class: AssetClass, currency: str | None) -> str | None:
    """Map a canonical symbol to a Trading 212 ticker, or None when unknown.

    US equities: ``AAPL`` -> ``AAPL_US_EQ``. Other markets need the instrument
    metadata cache: match ``shortName`` (or the ticker prefix) on the base symbol.
    """
    base = symbol.split(".")[0].upper()
    items = _items(_cache["items"])
    if asset_class is AssetClass.us_equity:
        ticker = f"{base}_US_EQ"
        if not items or any(i.get("ticker") == ticker for i in items):
            _reverse[ticker] = symbol
            return ticker
        return None
    if not items:
        return None
    hits = [
        i
        for i in items
        if i.get("type") in {"STOCK", "ETF"}
        and base in {str(i.get("shortName", "")).upper(), str(i.get("ticker", "")).split("_")[0]}
        and not str(i.get("ticker", "")).endswith("_US_EQ")
    ]
    if currency and len(hits) > 1:
        hits = [i for i in hits if i.get("currencyCode") == currency] or hits
    if len(hits) != 1:
        return None
    ticker = str(hits[0]["ticker"])
    _reverse[ticker] = symbol
    return ticker


def _canonical(ticker: str) -> str:
    """Our symbol for a Trading 212 ticker: learned from resolve_ticker, `X` for a
    `X_US_EQ` ticker, else the raw ticker (never a guessed symbol such as `SAPd`)."""
    if ticker in _reverse:
        return _reverse[ticker]
    if ticker.endswith("_US_EQ"):
        return ticker.removesuffix("_US_EQ")
    return ticker


@register
class T212DemoAdapter(BrokerAdapter):
    """Trading 212 demo. The order API has no client order reference, so placement is not
    idempotent: the engine must not blindly retry place_order after a timeout."""

    kind: ClassVar[str] = "t212_demo"
    supported_types: ClassVar[frozenset[OrderType]] = frozenset(
        {OrderType.market, OrderType.limit, OrderType.stop, OrderType.stop_limit}
    )
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(
        {AssetClass.us_equity, AssetClass.eu_equity}
    )

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        base = str(config.get("base_url") or DEMO_URL).rstrip("/")
        if base != DEMO_URL:
            raise SMTError(ErrorCode.FORBIDDEN, "t212_demo: only the demo endpoint is allowed")
        self.base_url = DEMO_URL
        cred = f"{config.get('api_key', '')}:{config.get('api_secret', '')}"
        self._headers = {
            "Authorization": "Basic " + base64.b64encode(cred.encode()).decode(),
            "Content-Type": "application/json",
        }

    idempotent_place: ClassVar[bool] = False

    def _supports_shape(self, order: Order, instrument: Instrument) -> bool:
        if not super().supports(order, instrument):
            return False
        return order.notional is None and order.qty is not None  # quantity orders only

    def supports(self, order: Order, instrument: Instrument) -> bool:
        if not self._supports_shape(order, instrument):
            return False
        return resolve_ticker(order.symbol, instrument.asset_class, instrument.currency) is not None

    def routable(self, order: Order, instrument: Instrument) -> bool:
        """Without metadata (a process that never loaded it) EU tickers cannot be
        resolved yet: route, BrokerSync checks `supports` after `prepare`."""
        if not self._supports_shape(order, instrument):
            return False
        if _cache["items"] is None:
            return True
        return self.supports(order, instrument)

    async def prepare(self, instruments: Iterable[Instrument] = ()) -> None:
        await self._load_instruments()
        for inst in instruments:
            resolve_ticker(inst.symbol, inst.asset_class, inst.currency)

    async def _request(
        self, method: str, path: str, *, content: str | None = None
    ) -> httpx.Response:
        try:
            resp = await self.http.request(
                method,
                self.base_url + path,
                headers=self._headers,
                content=content,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"t212_demo: {type(e).__name__}") from None
        if resp.status_code in {401, 403}:
            raise RuntimeError("t212_demo: credenziali non valide")
        if 300 <= resp.status_code < 400:  # never follow to another host
            raise RuntimeError(f"t212_demo: unexpected redirect HTTP {resp.status_code}")
        return resp

    @staticmethod
    def _parse(resp: httpx.Response):
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except ValueError:
            raise RuntimeError(
                f"t212_demo: HTTP {resp.status_code}: response is not JSON"
            ) from None

    def _fail(self, resp: httpx.Response) -> RuntimeError:
        return RuntimeError(f"t212_demo: HTTP {resp.status_code}: {_error_message(resp)}")

    @staticmethod
    def _cache_fresh() -> bool:
        return (
            _cache["items"] is not None
            and time.monotonic() - float(_cache["at"]) < _CACHE_TTL
        )

    async def _load_instruments(self) -> None:
        """Refresh the metadata cache when expired: one request at a time, and no new
        attempt within _REFRESH_BACKOFF_S of a failed one (stale data beats nothing)."""
        if self._cache_fresh():
            return
        async with _cache_lock:
            if self._cache_fresh():
                return
            failed_at = _cache.get("failed_at")
            if failed_at is not None and time.monotonic() - float(failed_at) < _REFRESH_BACKOFF_S:
                if _cache["items"] is not None:
                    return
                raise RuntimeError("t212_demo: instrument metadata unavailable (backing off)")
            try:
                resp = await self._request("GET", f"{_API}/metadata/instruments")
                if resp.status_code >= 300:
                    raise self._fail(resp)
                data = self._parse(resp)
            except RuntimeError:
                _cache["failed_at"] = time.monotonic()
                if _cache["items"] is not None:
                    return
                raise
            _cache["items"] = data if isinstance(data, list) else []
            _cache["at"] = time.monotonic()
            _cache["failed_at"] = None

    @staticmethod
    def _to_broker_order(data: object, fill_price: Decimal | None = None) -> BrokerOrder:
        if not isinstance(data, dict) or data.get("id") in (None, ""):
            raise RuntimeError("t212_demo: order response without an id")
        status = _STATUS.get(str(data.get("status")), "accepted")
        qty = _dec(data.get("filledQuantity")) or Decimal(0)
        return BrokerOrder(
            broker_order_id=str(data["id"]),
            status=status,
            filled_qty=abs(qty),
            avg_fill_price=fill_price,
            message=None,
            raw=_jsonable(data),
        )

    def _payload(self, order: Order, ticker: str) -> str:
        qty = order.qty if order.side == "buy" else -order.qty
        fields: dict = {"ticker": ticker, "quantity": qty}
        if order.type is not OrderType.market:
            fields["timeValidity"] = "DAY" if order.tif is TimeInForce.day else "GOOD_TILL_CANCEL"
        if order.limit_price is not None and order.type in {OrderType.limit, OrderType.stop_limit}:
            fields["limitPrice"] = order.limit_price
        if order.stop_price is not None and order.type in {OrderType.stop, OrderType.stop_limit}:
            fields["stopPrice"] = order.stop_price
        return _body(fields)

    async def place_order(self, order: Order, instrument: Instrument) -> BrokerOrder:
        await self._load_instruments()
        if not self.supports(order, instrument):
            return BrokerOrder("", "rejected", Decimal(0), None, "unsupported order", {})
        ticker = resolve_ticker(order.symbol, instrument.asset_class, instrument.currency)
        if ticker is None:
            return BrokerOrder("", "rejected", Decimal(0), None, "unknown instrument", {})
        resp = await self._request(
            "POST",
            f"{_API}/orders/{_ENDPOINT[order.type]}",
            content=self._payload(order, ticker),
        )
        if resp.status_code in _REJECT_HTTP:
            return BrokerOrder("", "rejected", Decimal(0), None, _error_message(resp), {})
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._to_broker_order(self._parse(resp))

    async def _from_history(self, broker_order_id: str) -> BrokerOrder | None:
        path = f"{_API}/history/orders?limit=50"
        for _ in range(_HISTORY_PAGES):
            resp = await self._request("GET", path)
            if resp.status_code >= 300:
                raise self._fail(resp)
            page = self._parse(resp)
            if not isinstance(page, dict):
                raise RuntimeError("t212_demo: unexpected history response")  # noqa: TRY004
            for it in page.get("items", []):
                o = it.get("order") or {}
                if str(o.get("id")) == broker_order_id:
                    fill = it.get("fill") or {}
                    return self._to_broker_order(o, _dec(fill.get("price")))
            path = page.get("nextPagePath")
            if not path:
                break
        return None

    async def find_order(
        self, order: Order, instrument: Instrument, since: datetime,
        exclude: frozenset[str] = frozenset(),
    ) -> BrokerOrder | None:
        """Ruling R7: the pending orders, then the first history page, for exactly one
        order on the same ticker, side and quantity created at or after `since`, not
        counting the broker orders in `exclude` (linked to other orders: C1)."""
        await self._load_instruments()
        ticker = resolve_ticker(order.symbol, instrument.asset_class, instrument.currency)
        if ticker is None or order.qty is None:
            return None
        signed = order.qty if order.side == "buy" else -order.qty

        def matches(o: dict) -> bool:
            t = o.get("ticker") or (o.get("instrument") or {}).get("ticker")
            qty = _dec(o.get("quantity"))
            side = str(o.get("side") or "").lower()
            if t != ticker or qty is None:
                return False
            if side and not (side == str(order.side) and abs(qty) == order.qty):
                return False
            if not side and qty != signed:
                return False
            try:
                created = datetime.fromisoformat(str(o.get("createdAt")))
            except ValueError:
                return False
            if created.tzinfo is None:
                return False
            return created >= since - _MATCH_SKEW

        resp = await self._request("GET", f"{_API}/orders")
        if resp.status_code >= 300:
            raise self._fail(resp)
        pending = self._parse(resp)
        hits = [
            self._to_broker_order(o)
            for o in (pending if isinstance(pending, list) else [])
            if isinstance(o, dict) and matches(o) and str(o.get("id")) not in exclude
        ]
        resp = await self._request("GET", f"{_API}/history/orders?limit=50")
        if resp.status_code >= 300:
            raise self._fail(resp)
        page = self._parse(resp)
        for it in page.get("items", []) if isinstance(page, dict) else []:
            o = it.get("order") or {}
            if matches(o) and str(o.get("id")) not in exclude | {
                h.broker_order_id for h in hits
            }:
                hits.append(self._to_broker_order(o, _dec((it.get("fill") or {}).get("price"))))
        return hits[0] if len(hits) == 1 else None

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        resp = await self._request("GET", f"{_API}/orders/{broker_order_id}")
        if resp.status_code == 404:  # only pending orders are served: look in the history
            found = await self._from_history(broker_order_id)
            if found is None:
                raise RuntimeError("t212_demo: order not found")
            return found
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._to_broker_order(self._parse(resp))

    async def cancel_order(self, broker_order_id: str) -> None:
        resp = await self._request("DELETE", f"{_API}/orders/{broker_order_id}")
        # a 4xx (404 unknown, 400 not cancelable...) means the order is already terminal
        # or cannot be canceled: BrokerSync reads it back and reflects its real state
        if resp.status_code >= 500:
            raise self._fail(resp)

    async def get_positions(self) -> dict[str, Decimal]:
        await self._load_instruments()
        resp = await self._request("GET", f"{_API}/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
        data = self._parse(resp)
        if not isinstance(data, list):
            raise RuntimeError("t212_demo: unexpected positions response")  # noqa: TRY004
        out: dict[str, Decimal] = {}
        for p in data:
            qty = _dec(p.get("quantity"))
            ticker = (p.get("instrument") or {}).get("ticker")
            if qty is None or not ticker:
                continue
            out[_canonical(str(ticker))] = qty
        return out

    async def health_check(self) -> None:
        resp = await self._request("GET", f"{_API}/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
