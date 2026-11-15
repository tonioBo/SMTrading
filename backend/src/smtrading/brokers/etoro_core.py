"""eToro public API protocol, shared by the demo and real adapters.

Docs: https://api-portal.etoro.com/llms.txt (consulted 2026-11-15). Demo and real share
one host (ETORO_URL): the guard is the path. Each concrete class declares its path
prefixes as ClassVars and `_guard_path` checks every request against them before it
touches the network. The real prefixes are written only in brokers/live/etoro_live.py.

Rules fixed by plan 9 (Task 13): market orders only; buys open unleveraged positions
(`transaction: buy`, `leverage: 1`, `settlementType: real`), sells close conforming
positions FIFO; never `sellShort`. Placement is not idempotent (no resend): the opening's
`x-request-id` is uuid5(ETORO_NS, client_ref) and comes back as `referenceId`, which
`find_order` looks up.

The module-level helpers (MARKET_DATA_PREFIX, etoro_headers, instrument_cache,
search_instrument, rate_headers, http_error) are reused by the market-data provider:
they hold no adapter state and touch no execution path.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar, Literal
from urllib.parse import unquote, urlencode

import httpx

from smtrading.brokers.base import (
    AccountSnapshot,
    BrokerAdapter,
    BrokerOrder,
    BrokerStatus,
    RateLimited,
)
from smtrading.brokers.t212_core import _body, _dec, _jsonable, _num
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import redact_log_text, register_secret
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.instruments.models import AssetClass, Instrument

ETORO_URL = "https://public-api.etoro.com"
# fixed namespace of the openings' x-request-id (never change: find_order relies on it)
ETORO_NS = uuid.UUID("5d7c1a3e-9b2f-4e86-a0d4-3c8e6f1b2a97")
MARKET_DATA_PREFIX = "/api/v1/market-data/"

FORBIDDEN_PATH_MESSAGE = "percorso non ammesso per questo collegamento"
MARKET_ONLY_MESSAGE = "tipo d'ordine non disponibile su questo broker: solo ordini a mercato"
QTY_ONLY_MESSAGE = "tipo d'ordine non disponibile su questo broker: solo quantità"
NOT_TRADABLE_MESSAGE = "strumento non negoziabile senza leva su questo broker"
NOT_CLOSABLE_MESSAGE = "quantità inferiore alla posizione chiudibile"
CURRENCY_MESSAGE = "valuta del conto non supportata"

_CACHE_TTL = 24 * 3600
_MAX_RATE_WAIT_S = 60.0
_MAX_CLOSES = 10
_ACCOUNT_POSITIONS_MAX_AGE_S = 5
_USD_ACCOUNT = 1  # accountCurrencyId
_REAL_SETTLEMENT = 1  # settlementTypeID of an owned asset
_REJECT_HTTP = {400, 422}
_SEARCH_FIELDS = "instrumentId,internalSymbolFull,isCurrentlyTradable,isBuyEnabled,isDelisted"
_SCOPES = ("demo", "real", "data")

# order status id (orders:lookup `status.id`) -> broker status (plan 9 Task 13)
_STATUS: dict[int, BrokerStatus] = {
    1: "accepted",  # Received
    2: "accepted",  # Placed
    3: "filled",
    4: "rejected",
    5: "partially_filled",
    6: "accepted",  # PendingCancel
    7: "canceled",
    8: "expired",
    9: "canceled",  # CanceledPartiallyFilled
    10: "canceled",  # RejectedPartiallyFilled: the filled part stays
    11: "accepted",  # WaitingForMarket
    12: "accepted",  # PendingTriggeredRate
}
_FINAL = frozenset({"filled", "canceled", "rejected", "expired"})

log = logging.getLogger("smtrading.brokers.etoro")


# --- shared helpers (no adapter state, no execution path) ------------------------


def etoro_headers(api_key: str, user_key: str, request_id: str | None = None) -> dict[str, str]:
    """The authentication headers of one request (a new uuid4 when `request_id` is None)."""
    return {
        "x-api-key": api_key,
        "x-user-key": user_key,
        "x-request-id": request_id or str(uuid.uuid4()),
    }


def account_digest(user_key: str) -> str:
    """A stable id of the account behind a user key (one key = one account and env)."""
    return hashlib.sha256(user_key.encode()).hexdigest()[:16]


@dataclass
class InstrumentCache:
    """Symbol -> {instrument_id, eligible, partial_close} of one scope ("demo", "real",
    "data"), each entry valid for 24 h. `reverse`: instrument id -> our symbol."""

    entries: dict[str, dict] = field(default_factory=dict)
    reverse: dict[int, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def fresh(self, symbol: str) -> dict | None:
        entry = self.entries.get(symbol)
        if entry is None or time.monotonic() - entry["at"] >= _CACHE_TTL:
            return None
        return entry

    def put(self, symbol: str, instrument_id: int | None, *, eligible: bool = False,
            partial_close: bool = False) -> dict:
        entry = {"instrument_id": instrument_id, "eligible": eligible,
                 "partial_close": partial_close, "at": time.monotonic()}
        self.entries[symbol] = entry
        return entry

    def clear(self) -> None:
        self.entries.clear()
        self.reverse.clear()


_instrument_caches: dict[str, InstrumentCache] = {}


def instrument_cache(scope: str) -> InstrumentCache:
    if scope not in _SCOPES:
        raise ValueError(f"unknown eToro cache scope {scope!r}")
    cache = _instrument_caches.get(scope)
    if cache is None:
        cache = _instrument_caches[scope] = InstrumentCache()
    return cache


def _search_path(symbol: str) -> str:
    query = urlencode({"internalSymbolFull": symbol, "fields": _SEARCH_FIELDS})
    return f"{MARKET_DATA_PREFIX}search?{query}"


def _get(d: dict, name: str) -> object:
    """`d[name]`, also in the other casing of a trailing ID (`positionID`/`positionId`):
    the schema and the examples of the docs differ."""
    if name in d:
        return d[name]
    if name.endswith("ID"):
        return d.get(name[:-2] + "Id")
    if name.endswith("Id"):
        return d.get(name[:-2] + "ID")
    return None


def _int(v: object) -> int | None:
    d = _dec(v)
    return int(d) if d is not None and d == d.to_integral_value() else None


def match_instrument(data: object, symbol: str) -> dict | None:
    """The `search` item whose `internalSymbolFull` is exactly `symbol` (the search also
    returns partial matches), or None."""
    items = data.get("items") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if (isinstance(item, dict) and str(item.get("internalSymbolFull", "")) == symbol
                and _int(_get(item, "instrumentId")) is not None):
            return item
    return None


def rate_headers(resp: httpx.Response) -> tuple[int | None, float | None]:
    """(`RateLimit-Remaining`, `RateLimit-Reset` seconds); on a 429 (0, `Retry-After`)."""
    remaining = _dec(resp.headers.get("RateLimit-Remaining"))
    reset = _dec(resp.headers.get("RateLimit-Reset"))
    if resp.status_code == 429:
        retry = _dec(resp.headers.get("Retry-After"))
        wait = retry if retry is not None else reset
        return 0, float(wait) if wait is not None else None
    return (
        int(remaining) if remaining is not None else None,
        float(reset) if reset is not None else None,
    )


def _body_message(resp: httpx.Response) -> str | None:
    """The error message of a response body, redacted (registered secrets masked),
    at most 200 characters; never a header."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    if not isinstance(body, dict):
        return None
    for key in ("errorMessage", "message", "error", "title", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return redact_log_text(value.strip())[:200]
    return None


def http_error(resp: httpx.Response) -> str:
    """«etoro: HTTP <code>» plus the redacted body message (never headers or keys)."""
    msg = _body_message(resp)
    return f"etoro: HTTP {resp.status_code}" + (f": {msg}" if msg else "")


def _parse(resp: httpx.Response) -> object:
    try:
        return json.loads(resp.content, parse_float=Decimal)
    except ValueError:
        raise RuntimeError(f"etoro: HTTP {resp.status_code}: response is not JSON") from None


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 429:
        raise RateLimited("etoro: limite di frequenza")
    if 300 <= resp.status_code < 400:  # never follow to another host
        raise RuntimeError(f"etoro: unexpected redirect HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise RuntimeError(http_error(resp))


async def search_instrument(http, headers: dict, symbol: str) -> dict | None:
    """GET market-data `search` for `symbol`: the exact `internalSymbolFull` match or
    None. Raises RateLimited on a 429 and RuntimeError on any other failure."""
    try:
        resp = await http.request("GET", ETORO_URL + _search_path(symbol), headers=headers,
                                  follow_redirects=False)
    except httpx.HTTPError as e:
        raise RuntimeError(f"etoro: {type(e).__name__}") from None
    _check(resp)
    return match_instrument(_parse(resp), symbol)


# --- eligibility and portfolio parsing --------------------------------------------


def _eligibility_items(data: object) -> list[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("instruments", "items", "results", "eligibility"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        if _get(data, "instrumentId") is not None:
            return [data]
    return []


def _buy_unleveraged_real(entry: dict) -> bool:
    if entry.get("allowOpenPosition") is not True:
        return False
    for cfg in entry.get("leverageConfigs") or []:
        if not isinstance(cfg, dict):
            continue
        values = cfg.get("leverageValues") or []
        if (str(cfg.get("settlementType", "")).lower() == "real"
                and str(cfg.get("direction", "")).lower() in {"buy", "long"}
                and any(_dec(v) == 1 for v in values)):
            return True
    return False


def _conforming(p: dict) -> bool:
    """An unleveraged owned long position, not copied (plan 9 Task 13)."""
    mirror = _get(p, "mirrorID")
    return (
        p.get("isBuy") is True
        and _dec(p.get("leverage")) == 1
        and _int(_get(p, "settlementTypeID")) == _REAL_SETTLEMENT
        and (mirror is None or _int(mirror) == 0)
    )


def _portfolio(data: object) -> dict:
    cp = data.get("clientPortfolio") if isinstance(data, dict) else None
    if not isinstance(cp, dict) or _dec(cp.get("credit")) is None:
        raise RuntimeError("etoro: unexpected account response")
    for key in ("positions", "orders", "ordersForOpen", "ordersForClose", "mirrors"):
        if not isinstance(cp.get(key, []), list):
            raise RuntimeError("etoro: unexpected account response")  # noqa: TRY004
    return cp


def _fifo(positions: list[dict]) -> list[dict]:
    """Oldest first: `openDateTime` when every position has one, else `positionID`."""
    def opened(p: dict) -> datetime | None:
        try:
            ts = datetime.fromisoformat(str(p.get("openDateTime")))
        except ValueError:
            return None
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    if positions and all(opened(p) is not None for p in positions):
        return sorted(positions, key=lambda p: (opened(p), _int(_get(p, "positionID")) or 0))
    return sorted(positions, key=lambda p: _int(_get(p, "positionID")) or 0)


_rate_blocked_until: dict[tuple[str, str, str], float] = {}


def _bucket(method: str, path: str) -> str:
    """The rate-limit family of a request (limits are per user key and family)."""
    if path.startswith(MARKET_DATA_PREFIX):
        return "market-data"
    if path.split("?", 1)[0].endswith("/eligibility"):
        return "eligibility"
    if method != "GET":
        return "write"
    if path.split("?", 1)[0].endswith("/pnl"):
        return "portfolio"
    return "orders"


class _EtoroProtocol(BrokerAdapter):
    """Payloads, status mapping, guard and requests of the eToro API. Concrete classes
    set `kind`, `BASE_URL`, `ENV`, the four path prefixes and `ALLOWED_PREFIXES`.

    Rate limits: when a family is exhausted (`RateLimit-Remaining: 0`), the next request
    of the family sleeps until `RateLimit-Reset` (at most 60 s) inside the adapter call;
    a 429 raises RateLimited. Callers must never hold a chain lock around adapter calls."""

    ENV: ClassVar[Literal["demo", "real"]]
    EXEC_V1: ClassVar[str]
    EXEC_V2: ClassVar[str]
    INFO_V1: ClassVar[str]
    INFO_V2: ClassVar[str]
    ALLOWED_PREFIXES: ClassVar[tuple[str, ...]]

    idempotent_place: ClassVar[bool] = False
    supported_types: ClassVar[frozenset[OrderType]] = frozenset({OrderType.market})
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(
        {AssetClass.us_equity, AssetClass.eu_equity, AssetClass.crypto}
    )

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        base = str(config.get("base_url") or self.BASE_URL).rstrip("/")
        if base != self.BASE_URL:
            raise SMTError(ErrorCode.FORBIDDEN, f"{self.kind}: base URL non consentito")
        self._api_key = str(config.get("api_key") or "")
        self._user_key = str(config.get("user_key") or "")
        for secret in (self._api_key, self._user_key):
            register_secret(secret)
        self._account = account_digest(self._user_key)
        self._account_positions: tuple[float, list[dict]] | None = None
        # injectable for tests
        self._sleep = asyncio.sleep
        self._clock = time.time

    # --- guard and requests ----------------------------------------------------

    def _guard_path(self, method: str, path: str) -> None:
        """Before any network I/O: the path must start with one of the class prefixes;
        a demo class only reaches market data or `/demo/` paths, a real class never a
        `/demo/` path; market data is GET only. Anything odd in the path is refused."""
        bare = path.split("?", 1)[0]
        decoded = unquote(bare)
        if (
            not bare.startswith("/")
            or "//" in decoded
            or "/." in decoded
            or "\\" in decoded
            or "#" in path
            or any(c.isspace() for c in path)
            or not bare.startswith(self.ALLOWED_PREFIXES)
        ):
            raise SMTError(ErrorCode.FORBIDDEN, FORBIDDEN_PATH_MESSAGE)
        market_data = bare.startswith(MARKET_DATA_PREFIX)
        if (
            (self.ENV == "demo" and not market_data and "/demo/" not in bare)
            or (self.ENV != "demo" and "/demo/" in bare)
            or (market_data and method.upper() != "GET")
        ):
            raise SMTError(ErrorCode.FORBIDDEN, FORBIDDEN_PATH_MESSAGE)

    def _rate_key(self, method: str, path: str) -> tuple[str, str, str]:
        return (self.ENV, self._account, _bucket(method, path))

    async def _respect_rate_limit(self, key: tuple[str, str, str]) -> None:
        until = _rate_blocked_until.get(key)
        if until is None:
            return
        wait = until - self._clock()
        if wait > 0:
            await self._sleep(min(wait, _MAX_RATE_WAIT_S))
        if _rate_blocked_until.get(key) == until:  # keep a newer block set meanwhile
            del _rate_blocked_until[key]

    def _note_rate_limit(self, key: tuple[str, str, str], resp: httpx.Response) -> None:
        remaining, reset = rate_headers(resp)
        if remaining is not None and remaining <= 0 and reset is not None and reset > 0:
            _rate_blocked_until[key] = self._clock() + reset

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        order: Order | None = None,
        close: dict | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        self._guard_path(method, path)
        key = self._rate_key(method, path)
        await self._respect_rate_limit(key)
        headers = etoro_headers(self._api_key, self._user_key, request_id)
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = _body(body)
        extensions: dict = {}
        if order is not None:
            extensions["smt_order_id"] = str(order.id)
        if close is not None:
            extensions["smt_close"] = close
        try:
            resp = await self.http.request(
                method, self.BASE_URL + path, headers=headers, content=content,
                follow_redirects=False, extensions=extensions or None,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"{self.kind}: {type(e).__name__}") from None
        self._note_rate_limit(key, resp)
        if resp.status_code == 429:  # not a rejection: the request was not processed
            raise RateLimited(f"{self.kind}: limite di frequenza")
        if resp.status_code in {401, 403}:
            raise RuntimeError(http_error(resp))
        if 300 <= resp.status_code < 400:
            raise RuntimeError(f"{self.kind}: unexpected redirect HTTP {resp.status_code}")
        return resp

    def _fail(self, resp: httpx.Response) -> RuntimeError:
        return RuntimeError(http_error(resp))

    async def _json(self, method: str, path: str, **kw) -> object:
        resp = await self._request(method, path, **kw)
        if resp.status_code >= 300:
            raise self._fail(resp)
        return _parse(resp)

    # --- instruments -----------------------------------------------------------

    @property
    def _instruments(self) -> InstrumentCache:
        return instrument_cache(self.ENV)

    @staticmethod
    def etoro_symbol(instrument: Instrument) -> str:
        """`provider_symbols["etoro"]`, else crypto `BTC-USD` -> `BTC`, else the symbol."""
        mapped = (instrument.provider_symbols or {}).get("etoro")
        if mapped:
            return str(mapped)
        if instrument.asset_class is AssetClass.crypto and instrument.symbol.endswith("-USD"):
            return instrument.symbol.removesuffix("-USD")
        return instrument.symbol

    def _entry(self, instrument: Instrument) -> dict | None:
        return self._instruments.fresh(self.etoro_symbol(instrument))

    async def prepare(self, instruments: Iterable[Instrument] = ()) -> None:
        """Search the instruments not cached (or expired), then one eligibility request
        for those found. Raises RuntimeError when eToro cannot answer (nothing cached)."""
        cache = self._instruments
        async with cache.lock:
            todo: dict[str, Instrument] = {}
            for inst in instruments:
                entry = self._entry(inst)
                if entry is None:
                    todo.setdefault(self.etoro_symbol(inst), inst)
                elif entry["instrument_id"] is not None:
                    cache.reverse[entry["instrument_id"]] = inst.symbol
            found: dict[str, tuple[int, dict, Instrument]] = {}
            for sym, inst in todo.items():
                item = match_instrument(await self._json("GET", _search_path(sym)), sym)
                if item is None:
                    cache.put(sym, None)
                    continue
                found[sym] = (int(_int(_get(item, "instrumentId"))), item, inst)
            if not found:
                return
            ids = [iid for iid, _, _ in found.values()]
            data = await self._json("POST", f"{self.INFO_V2}/eligibility",
                                    body={"instrumentIds": ids, "currency": "USD"})
            by_id = {_int(_get(e, "instrumentId")): e for e in _eligibility_items(data)}
            for sym, (iid, item, inst) in found.items():
                elig = by_id.get(iid) or {}
                eligible = (
                    item.get("isBuyEnabled") is True
                    and item.get("isDelisted") is not True
                    and _buy_unleveraged_real(elig)
                )
                cache.put(sym, iid, eligible=eligible,
                          partial_close=elig.get("allowPartialClosePosition") is True)
                cache.reverse[iid] = inst.symbol

    def _reason(self, order: Order, instrument: Instrument, *, strict: bool) -> str | None:
        if order.type not in self.supported_types:
            return MARKET_ONLY_MESSAGE
        if instrument.asset_class not in self.supported_asset_classes:
            return NOT_TRADABLE_MESSAGE
        if order.notional is not None:
            if order.side != OrderSide.buy or instrument.currency != "USD":
                return QTY_ONLY_MESSAGE
        elif order.qty is None or order.qty <= 0:
            return QTY_ONLY_MESSAGE
        entry = self._entry(instrument)
        if entry is None:
            return NOT_TRADABLE_MESSAGE if strict else None
        if entry["instrument_id"] is None:
            return NOT_TRADABLE_MESSAGE
        if order.side == OrderSide.buy and not entry["eligible"]:
            return NOT_TRADABLE_MESSAGE
        return None  # a sell closes positions: it needs only the instrument id

    def unsupported_reason(self, order: Order, instrument: Instrument) -> str | None:
        """Why the order cannot be routed (no network: optimistic when not cached)."""
        return self._reason(order, instrument, strict=False)

    def routable(self, order: Order, instrument: Instrument) -> bool:
        return self.unsupported_reason(order, instrument) is None

    def supports(self, order: Order, instrument: Instrument) -> bool:
        """After `prepare`: the instrument must be known (and eligible for a buy)."""
        return self._reason(order, instrument, strict=True) is None

    # --- orders ----------------------------------------------------------------

    def reference_id(self, order: Order) -> str:
        return str(uuid.uuid5(ETORO_NS, self.client_ref(order)))

    async def place_order(self, order: Order, instrument: Instrument) -> BrokerOrder:
        reason = self.unsupported_reason(order, instrument)  # no network for a bad shape
        if reason is None:
            await self.prepare([instrument])
            reason = self._reason(order, instrument, strict=True)
        if reason is not None:
            return BrokerOrder("", "rejected", Decimal(0), None, reason, {})
        entry = self._entry(instrument)
        assert entry is not None and entry["instrument_id"] is not None
        if order.side == OrderSide.sell:
            return await self._close(order, entry)
        fields: dict = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": entry["instrument_id"],
            "settlementType": "real",
            "orderType": "mkt",
            "leverage": 1,
        }
        if order.notional is not None:
            fields["amount"] = order.notional
        else:
            fields["units"] = order.qty
        resp = await self._request("POST", f"{self.EXEC_V2}/orders", body=fields,
                                   order=order, request_id=self.reference_id(order))
        if resp.status_code in _REJECT_HTTP:
            return BrokerOrder("", "rejected", Decimal(0), None, _body_message(resp), {})
        if resp.status_code >= 300:
            raise self._fail(resp)
        data = _parse(resp)
        oid = _get(data, "orderId") if isinstance(data, dict) else None
        if oid in (None, ""):
            raise RuntimeError(f"{self.kind}: order response without an id")
        return BrokerOrder(str(oid), "accepted", Decimal(0), None, None,
                           {k: _jsonable(v) for k, v in data.items() if k != "token"})

    async def _close(self, order: Order, entry: dict) -> BrokerOrder:
        """FIFO closes of the conforming positions of the instrument: whole positions up
        to the quantity (`UnitsToDeduct: null`), the last one partially only when the
        instrument allows it; at most 10 closes."""
        iid = entry["instrument_id"]
        cp = _portfolio(await self._json("GET", f"{self.INFO_V1}/pnl", order=order))
        positions = _fifo([p for p in cp.get("positions", [])
                           if isinstance(p, dict) and _conforming(p)
                           and _int(_get(p, "instrumentID")) == iid])
        remaining = order.qty
        plan: list[tuple[int, Decimal, bool]] = []  # (position id, units, whole)
        for p in positions:
            if remaining <= 0 or len(plan) == _MAX_CLOSES:
                break
            units, pid = _dec(p.get("units")), _int(_get(p, "positionID"))
            if units is None or units <= 0 or pid is None:
                continue
            if units <= remaining:
                plan.append((pid, units, True))
                remaining -= units
            else:
                if entry["partial_close"]:
                    plan.append((pid, remaining, False))
                    remaining = Decimal(0)
                break
        if not plan:
            return BrokerOrder("", "rejected", Decimal(0), None, NOT_CLOSABLE_MESSAGE, {})
        accepted: list[str] = []
        for pid, units, whole in plan:
            close = {"symbol": order.symbol, "instrument_id": iid, "units": _num(units)}
            try:
                resp = await self._request(
                    "POST", f"{self.EXEC_V1}/market-close-orders/positions/{pid}",
                    body={"InstrumentID": iid, "UnitsToDeduct": None if whole else units},
                    order=order, close=close,
                )
                if resp.status_code in _REJECT_HTTP and not accepted:
                    return BrokerOrder("", "rejected", Decimal(0), None,
                                       _body_message(resp), {})
                if resp.status_code >= 300:
                    raise self._fail(resp)
                data = _parse(resp)
                ofc = data.get("orderForClose") if isinstance(data, dict) else None
                cid = _get(ofc, "orderID") if isinstance(ofc, dict) else None
                if cid in (None, ""):
                    raise RuntimeError(f"{self.kind}: close response without an id")
            except RuntimeError as e:
                if not accepted:
                    raise
                # the accepted closes stand: the rest is a partial, reconciliation
                # covers any difference
                log.warning("order %s: close of position %s failed after %d accepted: %s",
                            order.id, pid, len(accepted), e)
                break
            accepted.append(str(cid))
        bid = f"close:{_num(order.qty)}:{','.join(accepted)}"
        return BrokerOrder(bid, "accepted", Decimal(0), None, None,
                           {"closes": accepted, "planned": len(plan)})

    async def find_order(
        self,
        order: Order,
        instrument: Instrument,
        since: datetime,
        exclude: frozenset[str] = frozenset(),
    ) -> BrokerOrder | None:
        """The opening of the order's current submission, by `referenceId`. Sells
        (position closes) have no reference: None."""
        if order.side != OrderSide.buy:
            return None
        query = urlencode({"referenceId": self.reference_id(order)})
        resp = await self._request("GET", f"{self.INFO_V2}/orders:lookup?{query}", order=order)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 300:
            raise self._fail(resp)
        bo = self._from_lookup(_parse(resp), "")
        if not bo.broker_order_id or bo.broker_order_id in exclude:
            return None
        return bo

    def _from_lookup(self, data: object, broker_order_id: str) -> BrokerOrder:
        if isinstance(data, dict) and isinstance(data.get("order"), dict):
            data = data["order"]
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            items = [o for o in data["orders"] if isinstance(o, dict)]
            data = items[0] if len(items) == 1 else None
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.kind}: unexpected order response")  # noqa: TRY004
        oid = _get(data, "orderId")
        bid = str(oid) if oid not in (None, "") else broker_order_id
        st = data.get("status")
        sid = _int(st.get("id") if isinstance(st, dict) else st)
        status: BrokerStatus = _STATUS.get(sid, "accepted") if sid is not None else "accepted"
        message = None
        asset = data.get("asset")
        if isinstance(asset, dict):
            lev, settle = asset.get("leverage"), asset.get("settlementType")
            if (lev is not None and _dec(lev) != 1) or (
                settle is not None and str(settle).lower() != "real"
            ):
                log.error("eToro order %s is not an unleveraged real buy (leverage %s, "
                          "settlement %s): treated as rejected", bid, lev, settle)
                status = "rejected"
        if status == "rejected" and isinstance(st, dict) and st.get("errorMessage"):
            message = redact_log_text(str(st["errorMessage"]))[:200]
        filled, cost, fees = Decimal(0), Decimal(0), None
        priced = True
        for ex in data.get("positionExecutions") or []:
            opening = ex.get("openingData") if isinstance(ex, dict) else None
            if not isinstance(opening, dict):
                continue
            units, price, fee = (_dec(opening.get("units")), _dec(opening.get("avgPrice")),
                                 _dec(opening.get("fees")))
            if units is None:
                continue
            filled += abs(units)
            if price is None:
                priced = False
            else:
                cost += abs(units) * price
            if fee is not None:
                fees = (fees or Decimal(0)) + abs(fee)
        avg = cost / filled if filled > 0 and priced else None
        return BrokerOrder(bid, status, filled, avg, message, _jsonable(data), fees)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        if broker_order_id.startswith("close:"):
            return await self._close_status(broker_order_id)
        query = urlencode({"orderId": broker_order_id})
        resp = await self._request("GET", f"{self.INFO_V2}/orders:lookup?{query}")
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._from_lookup(_parse(resp), broker_order_id)

    async def _close_status(self, broker_order_id: str) -> BrokerOrder:
        """The aggregate of the closes of a sell: all final -> filled when the units
        closed reach the quantity, else canceled with the partial (rejected when none
        closed and every close was rejected). A close reported filled without units
        keeps the sell open; without a price the fill waits (no invented price)."""
        try:
            _, qty_text, ids_text = broker_order_id.split(":", 2)
            qty = Decimal(qty_text)
        except (ValueError, ArithmeticError):
            raise RuntimeError(f"{self.kind}: unknown close order id") from None
        ids = [i for i in ids_text.split(",") if i]
        filled, cost, priced, final = Decimal(0), Decimal(0), True, True
        statuses: list[str] = []
        closes: list[dict] = []
        for cid in ids:
            data = await self._json("GET", f"{self.INFO_V1}/close-orders/{cid}")
            if not isinstance(data, dict):
                raise RuntimeError(  # noqa: TRY004
                    f"{self.kind}: unexpected close order response")
            closes.append(_jsonable(data))
            st = data.get("statusID", data.get("statusId", data.get("status")))
            if isinstance(st, dict):
                st = st.get("id")
            sid = _int(st)
            status = _STATUS.get(sid, "accepted") if sid is not None else "accepted"
            units = _dec(data.get("unitsDeducted"))
            rate = _dec(data.get("closeRate"))
            if units is None:
                if status == "filled":
                    log.warning("eToro close %s filled without units: waiting", cid)
                    final = False
                units = Decimal(0)
            if units:
                filled += abs(units)
                if rate is None:
                    priced = False
                else:
                    cost += abs(units) * rate
            if status not in _FINAL:
                final = False
            statuses.append(status)
        avg = cost / filled if filled > 0 and priced else None
        if not final:
            status = "partially_filled" if filled > 0 else "accepted"
        elif filled >= qty:
            status = "filled"
        elif filled == 0 and statuses and all(s == "rejected" for s in statuses):
            status = "rejected"
        else:
            status = "canceled"
        return BrokerOrder(broker_order_id, status, filled, avg, None, {"closes": closes})

    async def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id.startswith("close:"):
            return  # market closes cannot be canceled: BrokerSync reads their state
        resp = await self._request("DELETE", f"{self.EXEC_V2}/orders/{broker_order_id}")
        # a 4xx means not cancelable (already final): BrokerSync reads the real state
        if resp.status_code >= 500:
            raise self._fail(resp)

    # --- account ---------------------------------------------------------------

    async def _read_portfolio(self) -> dict:
        return _portfolio(await self._json("GET", f"{self.INFO_V1}/pnl"))

    def _positions(self, positions: list) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in positions:
            if not isinstance(p, dict) or not _conforming(p):
                continue
            iid, units = _int(_get(p, "instrumentID")), _dec(p.get("units"))
            if iid is None or units is None:
                continue
            symbol = self._instruments.reverse.get(iid, str(iid))
            out[symbol] = out.get(symbol, Decimal(0)) + units
        return out

    async def get_positions(self) -> dict[str, Decimal]:
        """The conforming positions, summed per symbol."""
        self._account_positions = None
        return self._positions((await self._read_portfolio()).get("positions", []))

    async def positions_after_account(self) -> dict[str, Decimal]:
        cached, self._account_positions = self._account_positions, None
        if cached is None or time.monotonic() - cached[0] > _ACCOUNT_POSITIONS_MAX_AGE_S:
            return await self.get_positions()
        return self._positions(cached[1])

    async def get_account(self) -> AccountSnapshot:
        """GET pnl: cash = credit − own pending openings − pending orders (USD accounts
        only); `cash` when every position conforms, else `leveraged`."""
        cp = await self._read_portfolio()
        if _int(cp.get("accountCurrencyId")) != _USD_ACCOUNT:
            raise RuntimeError(f"{self.kind}: {CURRENCY_MESSAGE}")
        positions = [p for p in cp.get("positions", []) if isinstance(p, dict)]
        mirrors = cp.get("mirrors", [])
        pending_open = sum(
            (_dec(o.get("amount")) or Decimal(0)
             for o in cp.get("ordersForOpen", [])
             if isinstance(o, dict) and (_int(_get(o, "mirrorID")) or 0) == 0),
            Decimal(0),
        )
        pending = sum((_dec(o.get("amount")) or Decimal(0)
                       for o in cp.get("orders", []) if isinstance(o, dict)), Decimal(0))
        cash = _dec(cp.get("credit")) - pending_open - pending
        self._account_positions = (time.monotonic(), positions)
        conforming = all(_conforming(p) for p in positions) and not mirrors
        return AccountSnapshot(
            account_id=self._account,
            account_type="cash" if conforming else "leveraged",
            currency="USD",
            cash_available=max(cash, Decimal(0)),
            positions_count=len(positions) + len(mirrors),
            open_orders_count=sum(len(cp.get(k, [])) for k in
                                  ("orders", "ordersForOpen", "ordersForClose")),
            crypto_enabled=None,
            order_types=frozenset(self.supported_types),
            raw={"credit": _jsonable(cp.get("credit")),
                 "accountCurrencyId": cp.get("accountCurrencyId")},
        )

    async def health_check(self) -> None:
        await self._read_portfolio()
