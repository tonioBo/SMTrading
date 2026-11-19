"""Trading 212 public API protocol, shared by the demo and live adapters.

Official docs: https://docs.trading212.com/api (OpenAPI bundle
https://docs.trading212.com/_bundle/api.yaml). The base URL is a ClassVar constant of
the concrete class, never configuration.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx

from smtrading.brokers.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOpenOrder,
    BrokerOrder,
    BrokerStatus,
    RateLimited,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

_API = "/api/v0/equity"
_CACHE_TTL = 24 * 3600
_ACCOUNT_POSITIONS_MAX_AGE_S = 5  # positions_after_account reuses a read this recent
_REFRESH_BACKOFF_S = 60  # the metadata endpoint allows 1 request / 50s
_HISTORY_PAGES = 5
_MATCH_SKEW = timedelta(seconds=60)  # clock skew allowed when matching createdAt
_MAX_RATE_WAIT_S = 60.0  # never wait longer than this for a rate-limit reset
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")

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


@dataclass
class InstrumentCache:
    """In-process instrument metadata of one environment (BASE_URL), since the endpoint
    allows 1 request / 50s. Keyed by environment only: the instrument universe is the
    platform's, not the account's. Never shared across environments, so a demo failure or
    backoff cannot affect another environment and ticker lists never cross."""

    data: dict[str, object] = field(
        default_factory=lambda: {"at": 0.0, "items": None, "failed_at": None}
    )
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # ticker -> canonical symbol, learned from resolve_ticker (positions use it)
    reverse: dict[str, str] = field(default_factory=dict)


_instrument_caches: dict[str, InstrumentCache] = {}


def instrument_cache(base_url: str) -> InstrumentCache:
    cache = _instrument_caches.get(base_url)
    if cache is None:
        cache = _instrument_caches[base_url] = InstrumentCache()
    return cache


# (base URL, credential digest, endpoint) -> unix time before which the endpoint is
# exhausted (x-ratelimit-remaining == 0 until x-ratelimit-reset). Limits are per account
# and per endpoint, so the state outlives a single adapter instance.
_rate_blocked_until: dict[tuple[str, str, str], float] = {}
log = logging.getLogger("smtrading.brokers.t212")


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


def resolve_ticker(
    symbol: str, asset_class: AssetClass, currency: str | None, *, cache: InstrumentCache
) -> str | None:
    """Map a canonical symbol to a Trading 212 ticker, or None when unknown.

    US equities: ``AAPL`` -> ``AAPL_US_EQ``. Other markets need the instrument
    metadata cache of the environment: match ``shortName`` (or the ticker prefix) on the
    base symbol.
    """
    base = symbol.split(".")[0].upper()
    items = _items(cache.data["items"])
    if asset_class is AssetClass.us_equity:
        ticker = f"{base}_US_EQ"
        if not items or any(i.get("ticker") == ticker for i in items):
            cache.reverse[ticker] = symbol
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
    cache.reverse[ticker] = symbol
    return ticker


def _canonical(ticker: str, cache: InstrumentCache) -> str:
    """Our symbol for a Trading 212 ticker: learned from resolve_ticker, `X` for a
    `X_US_EQ` ticker, else the raw ticker (never a guessed symbol such as `SAPd`)."""
    if ticker in cache.reverse:
        return cache.reverse[ticker]
    if ticker.endswith("_US_EQ"):
        return ticker.removesuffix("_US_EQ")
    return ticker


def _reset_epoch(value: str | None) -> float | None:
    """`x-ratelimit-reset` is a Unix timestamp (seconds; milliseconds tolerated)."""
    d = _dec(value)
    if d is None or d <= 0:
        return None
    ts = float(d)
    return ts / 1000 if ts > 1e11 else ts


def _fees(fill: object) -> tuple[Decimal | None, list[dict]]:
    """Fees of a fill in the account currency: the sum of `walletImpact.taxes`
    (currency conversion fee, transaction taxes...) charged in that currency, and the
    taxes left out because charged in another currency. The API documents no rate for
    them (`walletImpact.fxRate` is the trade's rate, its direction undocumented), so they
    are never converted: excluded, logged and reported in BrokerOrder.raw."""
    impact = fill.get("walletImpact") if isinstance(fill, dict) else None
    if not isinstance(impact, dict) or not isinstance(impact.get("taxes"), list):
        return None, []
    currency = impact.get("currency")
    total = Decimal(0)
    excluded: list[dict] = []
    for tax in impact["taxes"]:
        if not isinstance(tax, dict):
            continue
        if currency and tax.get("currency") and tax.get("currency") != currency:
            excluded.append(_jsonable(tax))
            continue
        q = _dec(tax.get("quantity"))
        if q is not None:
            total += abs(q)
    if excluded:
        log.warning(
            "trading 212 fill taxes not in the account currency %s, excluded from fees: %s",
            currency,
            ", ".join(f"{t.get('name')} {t.get('currency')}" for t in excluded),
        )
    return total, excluded


class _T212Protocol(BrokerAdapter):
    """Payloads, status mapping and requests of the Trading 212 API. The order API has no
    client order reference, so placement is not idempotent: the engine must not blindly
    retry place_order after a timeout. Concrete classes set `kind` and `BASE_URL`.

    Rate limits: when an endpoint is exhausted (`x-ratelimit-remaining == 0`), the next
    request to it sleeps until `x-ratelimit-reset` (at most _MAX_RATE_WAIT_S) inside the
    adapter call, so a BrokerSync pass can be delayed by that wait. Callers must never
    hold a chain lock around adapter calls (R9)."""

    supported_types: ClassVar[frozenset[OrderType]] = frozenset(
        {OrderType.market, OrderType.limit, OrderType.stop, OrderType.stop_limit}
    )
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(
        {AssetClass.us_equity, AssetClass.eu_equity}
    )

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        base = str(config.get("base_url") or self.BASE_URL).rstrip("/")
        if base != self.BASE_URL:
            raise SMTError(ErrorCode.FORBIDDEN, f"{self.kind}: base URL non consentito")
        cred = f"{config.get('api_key', '')}:{config.get('api_secret', '')}"
        self._headers = {
            "Authorization": "Basic " + base64.b64encode(cred.encode()).decode(),
            "Content-Type": "application/json",
        }
        self._rate_account = hashlib.sha256(cred.encode()).hexdigest()[:16]
        # injectable for tests
        self._sleep = asyncio.sleep
        self._clock = time.time

    idempotent_place: ClassVar[bool] = False

    def _supports_shape(self, order: Order, instrument: Instrument) -> bool:
        if not super().supports(order, instrument):
            return False
        return order.notional is None and order.qty is not None  # quantity orders only

    def supports(self, order: Order, instrument: Instrument) -> bool:
        if not self._supports_shape(order, instrument):
            return False
        return self._ticker(order.symbol, instrument) is not None

    @property
    def _instruments(self) -> InstrumentCache:
        return instrument_cache(self.BASE_URL)

    def _ticker(self, symbol: str, instrument: Instrument) -> str | None:
        return resolve_ticker(
            symbol, instrument.asset_class, instrument.currency, cache=self._instruments
        )

    def routable(self, order: Order, instrument: Instrument) -> bool:
        """Without metadata (a process that never loaded it) EU tickers cannot be
        resolved yet: route, BrokerSync checks `supports` after `prepare`."""
        if not self._supports_shape(order, instrument):
            return False
        if self._instruments.data["items"] is None:
            return True
        return self.supports(order, instrument)

    async def prepare(self, instruments: Iterable[Instrument] = ()) -> None:
        await self._load_instruments()
        for inst in instruments:
            self._ticker(inst.symbol, inst)

    def _rate_key(self, method: str, path: str) -> tuple[str, str, str]:
        endpoint = _ID_SEGMENT.sub("/{id}", path.split("?", 1)[0])
        return (self.BASE_URL, self._rate_account, f"{method} {endpoint}")

    async def _respect_rate_limit(self, key: tuple[str, str, str]) -> None:
        until = _rate_blocked_until.get(key)
        if until is None:
            return
        wait = until - self._clock()
        if wait > 0:
            await self._sleep(min(wait, _MAX_RATE_WAIT_S))
        # a request answered while we slept may have set a newer block: keep it
        if _rate_blocked_until.get(key) == until:
            del _rate_blocked_until[key]

    def _note_rate_limit(self, key: tuple[str, str, str], resp: httpx.Response) -> None:
        """x-ratelimit-remaining == 0: wait for x-ratelimit-reset before the next request
        to the same endpoint."""
        remaining = _dec(resp.headers.get("x-ratelimit-remaining"))
        if (remaining is not None and remaining <= 0) or resp.status_code == 429:
            reset = _reset_epoch(resp.headers.get("x-ratelimit-reset"))
            if reset is not None:
                _rate_blocked_until[key] = reset

    async def _request(
        self, method: str, path: str, *, content: str | None = None, order: Order | None = None
    ) -> httpx.Response:
        key = self._rate_key(method, path)
        await self._respect_rate_limit(key)
        try:
            resp = await self.http.request(
                method,
                self.BASE_URL + path,
                headers=self._headers,
                content=content,
                follow_redirects=False,
                extensions={"smt_order_id": str(order.id)} if order is not None else None,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"{self.kind}: {type(e).__name__}") from None
        self._note_rate_limit(key, resp)
        if resp.status_code == 429:  # not a rejection: the request was not processed
            raise RateLimited(f"{self.kind}: limite di frequenza Trading 212")
        if resp.status_code in {401, 403}:
            raise RuntimeError(f"{self.kind}: credenziali non valide")
        if 300 <= resp.status_code < 400:  # never follow to another host
            raise RuntimeError(f"{self.kind}: unexpected redirect HTTP {resp.status_code}")
        return resp

    def _parse(self, resp: httpx.Response):
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except ValueError:
            raise RuntimeError(
                f"{self.kind}: HTTP {resp.status_code}: response is not JSON"
            ) from None

    def _fail(self, resp: httpx.Response) -> RuntimeError:
        return RuntimeError(f"{self.kind}: HTTP {resp.status_code}: {_error_message(resp)}")

    def _cache_fresh(self) -> bool:
        data = self._instruments.data
        return data["items"] is not None and time.monotonic() - float(data["at"]) < _CACHE_TTL

    async def _load_instruments(self) -> None:
        """Refresh the metadata cache when expired: one request at a time, and no new
        attempt within _REFRESH_BACKOFF_S of a failed one (stale data beats nothing)."""
        if self._cache_fresh():
            return
        _cache = self._instruments.data
        async with self._instruments.lock:
            if self._cache_fresh():
                return
            failed_at = _cache.get("failed_at")
            if failed_at is not None and time.monotonic() - float(failed_at) < _REFRESH_BACKOFF_S:
                if _cache["items"] is not None:
                    return
                raise RuntimeError(f"{self.kind}: instrument metadata unavailable (backing off)")
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

    def _to_broker_order(
        self, data: object, fill_price: Decimal | None = None, fill: object = None
    ) -> BrokerOrder:
        if not isinstance(data, dict) or data.get("id") in (None, ""):
            raise RuntimeError(f"{self.kind}: order response without an id")
        status = _STATUS.get(str(data.get("status")), "accepted")
        qty = _dec(data.get("filledQuantity")) or Decimal(0)
        raw = _jsonable(data)
        fees, unconverted = _fees(fill)
        if unconverted:
            raw["smt_unconverted_taxes"] = unconverted
        return BrokerOrder(
            broker_order_id=str(data["id"]),
            status=status,
            filled_qty=abs(qty),
            avg_fill_price=fill_price,
            message=None,
            raw=raw,
            fees=fees,
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
        ticker = self._ticker(order.symbol, instrument)
        if ticker is None:
            return BrokerOrder("", "rejected", Decimal(0), None, "unknown instrument", {})
        resp = await self._request(
            "POST",
            f"{_API}/orders/{_ENDPOINT[order.type]}",
            content=self._payload(order, ticker),
            order=order,
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
                raise RuntimeError(f"{self.kind}: unexpected history response")  # noqa: TRY004
            for it in page.get("items", []):
                o = it.get("order") or {}
                if str(o.get("id")) == broker_order_id:
                    fill = it.get("fill") or {}
                    return self._to_broker_order(o, _dec(fill.get("price")), fill)
            path = page.get("nextPagePath")
            if not path:
                break
        return None

    async def find_order(
        self,
        order: Order,
        instrument: Instrument,
        since: datetime,
        exclude: frozenset[str] = frozenset(),
    ) -> BrokerOrder | None:
        """Ruling R7: the pending orders, then the first history page, for exactly one
        order on the same ticker, side and quantity created at or after `since`, not
        counting the broker orders in `exclude` (linked to other orders: C1)."""
        await self._load_instruments()
        ticker = self._ticker(order.symbol, instrument)
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

        resp = await self._request("GET", f"{_API}/orders", order=order)
        if resp.status_code >= 300:
            raise self._fail(resp)
        pending = self._parse(resp)
        hits = [
            self._to_broker_order(o)
            for o in (pending if isinstance(pending, list) else [])
            if isinstance(o, dict) and matches(o) and str(o.get("id")) not in exclude
        ]
        resp = await self._request("GET", f"{_API}/history/orders?limit=50", order=order)
        if resp.status_code >= 300:
            raise self._fail(resp)
        page = self._parse(resp)
        for it in page.get("items", []) if isinstance(page, dict) else []:
            o = it.get("order") or {}
            if matches(o) and str(o.get("id")) not in exclude | {h.broker_order_id for h in hits}:
                fill = it.get("fill") or {}
                hits.append(self._to_broker_order(o, _dec(fill.get("price")), fill))
        return hits[0] if len(hits) == 1 else None

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        resp = await self._request("GET", f"{_API}/orders/{broker_order_id}")
        if resp.status_code == 404:  # only pending orders are served: look in the history
            found = await self._from_history(broker_order_id)
            if found is None:
                raise RuntimeError(f"{self.kind}: order not found")
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
        self._account_positions = None  # a newer read: the account's one is stale
        resp = await self._request("GET", f"{_API}/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._positions(self._parse(resp))

    async def positions_after_account(self) -> dict[str, Decimal]:
        """The positions `get_account` just read (Trading 212 rate limits every read)."""
        cached = getattr(self, "_account_positions", None)
        self._account_positions = None  # consumed: never reused
        if cached is None or time.monotonic() - cached[0] > _ACCOUNT_POSITIONS_MAX_AGE_S:
            return await self.get_positions()
        await self._load_instruments()
        return self._positions(cached[1])

    def _positions(self, data) -> dict[str, Decimal]:
        if not isinstance(data, list):
            raise RuntimeError(f"{self.kind}: unexpected positions response")  # noqa: TRY004
        out: dict[str, Decimal] = {}
        for p in data:
            qty = _dec(p.get("quantity"))
            ticker = (p.get("instrument") or {}).get("ticker")
            if qty is None or not ticker:
                continue
            out[_canonical(str(ticker), self._instruments)] = qty
        return out

    async def get_open_orders(self) -> list[BrokerOpenOrder]:
        """GET orders (the pending ones): a sell has a negative `quantity` (or `side`
        sell); the open quantity is |quantity| − |filledQuantity|."""
        await self._load_instruments()
        resp = await self._request("GET", f"{_API}/orders")
        if resp.status_code >= 300:
            raise self._fail(resp)
        data = self._parse(resp)
        if not isinstance(data, list):
            raise RuntimeError(f"{self.kind}: unexpected orders response")  # noqa: TRY004
        out: list[BrokerOpenOrder] = []
        for o in data:
            if not isinstance(o, dict) or o.get("id") in (None, ""):
                continue
            ticker = o.get("ticker") or (o.get("instrument") or {}).get("ticker")
            qty = _dec(o.get("quantity"))
            if not ticker or qty is None:
                continue
            side = str(o.get("side") or "").lower()
            sell = side == "sell" if side else qty < 0
            open_qty = max(abs(qty) - abs(_dec(o.get("filledQuantity")) or Decimal(0)),
                           Decimal(0))
            out.append(BrokerOpenOrder(
                broker_order_id=str(o["id"]),
                symbol=_canonical(str(ticker), self._instruments),
                side="sell" if sell else "buy",
                qty=open_qty,
                limit_price=_dec(o.get("limitPrice")),
                stop_price=_dec(o.get("stopPrice")),
            ))
        return out

    async def get_account(self) -> AccountSnapshot:
        """GET account/summary (`id`, `currency`, `cash.availableToTrade`), plus the counts
        of the open positions and the pending orders."""
        resp = await self._request("GET", f"{_API}/account/summary")
        if resp.status_code >= 300:
            raise self._fail(resp)
        acct = self._parse(resp)
        cash = (acct.get("cash") or {}) if isinstance(acct, dict) else {}
        available = _dec(cash.get("availableToTrade")) if isinstance(cash, dict) else None
        if (
            not isinstance(acct, dict)
            or acct.get("id") in (None, "")
            or not acct.get("currency")
            or available is None
        ):
            raise RuntimeError(f"{self.kind}: unexpected account response")
        resp = await self._request("GET", f"{_API}/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
        positions = self._parse(resp)
        resp = await self._request("GET", f"{_API}/orders")
        if resp.status_code >= 300:
            raise self._fail(resp)
        orders = self._parse(resp)
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise RuntimeError(f"{self.kind}: unexpected account response")  # noqa: TRY004
        self._account_positions = (time.monotonic(), positions)  # positions_after_account
        return AccountSnapshot(
            account_id=str(acct["id"]),
            account_type=None,
            currency=str(acct["currency"]),
            cash_available=max(available, Decimal(0)),
            positions_count=len(positions),
            open_orders_count=len(orders),
            crypto_enabled=None,
            order_types=None,
            raw=_jsonable(acct),
        )

    async def health_check(self) -> None:
        resp = await self._request("GET", f"{_API}/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
