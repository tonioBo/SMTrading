"""Alpaca trading API protocol, shared by the paper and live adapters.

Official docs: https://docs.alpaca.markets/reference (orders, positions, account).
The base URL is a ClassVar constant of the concrete class, never configuration.

Fractional quantities and notional orders (equities): Alpaca takes them only as `day`
orders and never as a trailing stop (https://docs.alpaca.markets/docs/fractional-trading;
the paper API applies the same rules). A `gtc` one is sent as `day`; a resting one
(limit, stop, stop limit) is renewed by BrokerSync at the next session after the broker
expires it (`renewed_daily`), so a fractional bracket exit stays protected. A market
order fills at once: it is sent as `day` and never renewed.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx

from smtrading.brokers.base import AccountSnapshot, BrokerAdapter, BrokerOrder, BrokerStatus
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

_CRYPTO_QUOTES = ("USDT", "USDC", "USD")

_STATUS: dict[str, BrokerStatus] = {
    "new": "accepted",
    "accepted": "accepted",
    "pending_new": "accepted",
    "accepted_for_bidding": "accepted",
    "pending_cancel": "accepted",
    "pending_replace": "accepted",
    "held": "accepted",
    "calculated": "accepted",
    "done_for_day": "accepted",
    "stopped": "accepted",
    "suspended": "accepted",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "replaced": "canceled",
    "expired": "expired",
    "rejected": "rejected",
}
_REJECT_HTTP = {403, 422}
_AUTH_WORDS = ("forbidden", "unauthorized", "access key")
_DUP_WORDS = ("client_order_id", "unique", "duplicate")
_OPEN_ORDERS_LIMIT = 500  # the API maximum
# resting order types BrokerSync renews every session when Alpaca takes them only as day
_RENEWED_TYPES = frozenset({OrderType.limit, OrderType.stop, OrderType.stop_limit})


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


def _fractional(order: Order) -> bool:
    return order.qty is not None and order.qty != order.qty.to_integral_value()


def to_alpaca_symbol(canonical: str, asset_class: AssetClass) -> str:
    if asset_class is AssetClass.crypto:
        return canonical.replace("-", "/")
    return canonical


def from_alpaca_symbol(symbol: str, asset_class: str | None) -> str:
    if "/" in symbol:
        return symbol.replace("/", "-")
    if asset_class == "crypto":  # positions report BTCUSD without a separator
        for q in _CRYPTO_QUOTES:
            if symbol.endswith(q) and len(symbol) > len(q):
                return f"{symbol[: -len(q)]}-{q}"
    return symbol


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    return str(msg) if msg else "error"


class _AlpacaProtocol(BrokerAdapter):
    """Payloads, status mapping and requests of the Alpaca trading API. Concrete
    classes set `kind` and `BASE_URL`."""

    supported_types: ClassVar[frozenset[OrderType]] = frozenset(OrderType)
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(
        {AssetClass.us_equity, AssetClass.crypto}
    )

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        base = str(config.get("base_url") or self.BASE_URL).rstrip("/")
        if base != self.BASE_URL:
            raise SMTError(ErrorCode.FORBIDDEN, f"{self.kind}: base URL non consentito")
        self._headers = {
            "APCA-API-KEY-ID": str(config.get("key_id", "")),
            "APCA-API-SECRET-KEY": str(config.get("secret_key", "")),
        }

    def supports(self, order: Order, instrument: Instrument) -> bool:
        if not super().supports(order, instrument):
            return False
        if instrument.asset_class is AssetClass.crypto:
            # Alpaca crypto: market, limit, stop_limit only
            return order.type in {OrderType.market, OrderType.limit, OrderType.stop_limit}
        if order.notional is not None:  # notional only for market orders
            return order.type is OrderType.market
        if _fractional(order):  # fractional shares: never a trailing stop
            return order.type is not OrderType.trailing_stop
        return True

    @staticmethod
    def _day_only(order: Order, instrument: Instrument) -> bool:
        """Alpaca takes the order only as `day` (fractional or notional equity)."""
        return instrument.asset_class is not AssetClass.crypto and (
            order.notional is not None or _fractional(order))

    def renewed_daily(self, order: Order, instrument: Instrument) -> bool:
        return (order.tif is TimeInForce.gtc and order.type in _RENEWED_TYPES
                and self._day_only(order, instrument))

    def _tif(self, order: Order, instrument: Instrument) -> str:
        if instrument.asset_class is AssetClass.crypto:
            return "gtc"
        return "day" if self._day_only(order, instrument) else str(order.tif)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        order: Order | None = None,
    ) -> httpx.Response:
        try:
            return await self.http.request(
                method,
                self.BASE_URL + path,
                headers=self._headers,
                json=json_body,
                params=params,
                follow_redirects=False,
                extensions={"smt_order_id": str(order.id)} if order is not None else None,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"{self.kind}: {type(e).__name__}") from None

    @staticmethod
    def _parse(resp: httpx.Response):
        return json.loads(resp.content, parse_float=Decimal)

    def _fail(self, resp: httpx.Response) -> RuntimeError:
        return RuntimeError(f"{self.kind}: HTTP {resp.status_code}: {_error_message(resp)}")

    @staticmethod
    def _to_broker_order(data: dict) -> BrokerOrder:
        status = _STATUS.get(str(data.get("status")), "accepted")
        return BrokerOrder(
            broker_order_id=str(data["id"]),
            status=status,
            filled_qty=_dec(data.get("filled_qty")) or Decimal(0),
            avg_fill_price=_dec(data.get("filled_avg_price")),
            message=None,
            raw=json.loads(json.dumps(data, default=str)),
            # the order entity carries no fee; a `commission` (regulatory fees on
            # sells, where reported) counts as the broker-reported fee
            fees=_dec(data.get("commission")),
        )

    def _payload(self, order: Order, instrument: Instrument) -> dict:
        body: dict = {
            "symbol": to_alpaca_symbol(order.symbol, instrument.asset_class),
            "side": str(order.side),
            "type": "stop_limit" if order.type is OrderType.stop_limit else str(order.type),
            "time_in_force": self._tif(order, instrument),
            "client_order_id": self.client_ref(order),
        }
        if order.qty is not None:
            body["qty"] = _num(order.qty)
        if order.notional is not None:
            body["notional"] = _num(order.notional)
        if order.limit_price is not None:
            body["limit_price"] = _num(order.limit_price)
        if order.stop_price is not None:
            body["stop_price"] = _num(order.stop_price)
        if order.type is OrderType.trailing_stop:
            if order.trail_pct is not None:
                body["trail_percent"] = _num(order.trail_pct)
            elif order.trail_amount is not None:
                body["trail_price"] = _num(order.trail_amount)
        return body

    async def place_order(self, order: Order, instrument: Instrument) -> BrokerOrder:
        body = self._payload(order, instrument)
        resp = await self._request("POST", "/v2/orders", json_body=body, order=order)
        if resp.status_code in _REJECT_HTTP:
            msg = _error_message(resp)
            low = msg.lower()
            if resp.status_code == 403 and any(w in low for w in _AUTH_WORDS):
                raise RuntimeError(f"{self.kind}: credenziali non valide")
            if resp.status_code == 422 and any(w in low for w in _DUP_WORDS):
                existing = await self._request(
                    "GET",
                    "/v2/orders:by_client_order_id",
                    params={"client_order_id": body["client_order_id"]},
                    order=order,
                )
                if existing.status_code < 300:
                    return self._to_broker_order(self._parse(existing))
            return BrokerOrder("", "rejected", Decimal(0), None, msg, {})
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._to_broker_order(self._parse(resp))

    async def find_order(
        self, order: Order, instrument: Instrument, since, exclude: frozenset[str] = frozenset()
    ) -> BrokerOrder | None:
        """The order sent with this submission's client_order_id, if Alpaca has it (only
        the current one: earlier submissions were confirmed canceled before a resend)."""
        resp = await self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": self.client_ref(order)},
            order=order,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 300:
            raise self._fail(resp)
        found = self._to_broker_order(self._parse(resp))
        return None if found.broker_order_id in exclude else found

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        resp = await self._request("GET", f"/v2/orders/{broker_order_id}")
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._to_broker_order(self._parse(resp))

    async def cancel_order(self, broker_order_id: str) -> None:
        resp = await self._request("DELETE", f"/v2/orders/{broker_order_id}")
        # 404 unknown / 422 no longer cancelable: already terminal, the sync reflects it
        if resp.status_code >= 300 and resp.status_code not in {404, 422}:
            raise self._fail(resp)

    async def get_positions(self) -> dict[str, Decimal]:
        resp = await self._request("GET", "/v2/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
        out: dict[str, Decimal] = {}
        for p in self._parse(resp):
            qty = _dec(p.get("qty"))
            if qty is None:
                continue
            if p.get("side") == "short" and qty > 0:
                qty = -qty
            out[from_alpaca_symbol(str(p["symbol"]), p.get("asset_class"))] = qty
        return out

    async def get_account(self) -> AccountSnapshot:
        """GET /v2/account, plus the counts of /v2/positions and open /v2/orders.
        Available cash: `cash`, capped by `buying_power` (never margin)."""
        resp = await self._request("GET", "/v2/account")
        if resp.status_code >= 300:
            raise self._fail(resp)
        acct = self._parse(resp)
        if not isinstance(acct, dict) or not acct.get("id") or _dec(acct.get("cash")) is None:
            raise RuntimeError(f"{self.kind}: unexpected account response")
        cash = _dec(acct.get("cash")) or Decimal(0)
        buying_power = _dec(acct.get("buying_power"))
        if buying_power is not None:
            cash = min(cash, buying_power)
        multiplier = _dec(acct.get("multiplier"))
        resp = await self._request("GET", "/v2/positions")
        if resp.status_code >= 300:
            raise self._fail(resp)
        positions = self._parse(resp)
        resp = await self._request(
            "GET", "/v2/orders", params={"status": "open", "limit": _OPEN_ORDERS_LIMIT}
        )
        if resp.status_code >= 300:
            raise self._fail(resp)
        orders = self._parse(resp)
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise RuntimeError(f"{self.kind}: unexpected account response")  # noqa: TRY004
        crypto = acct.get("crypto_status")
        return AccountSnapshot(
            account_id=str(acct["id"]),
            account_type=None if multiplier is None else ("margin" if multiplier > 1 else "cash"),
            currency=str(acct.get("currency") or "USD"),
            cash_available=max(cash, Decimal(0)),
            positions_count=len(positions),
            open_orders_count=len(orders),
            crypto_enabled=None if crypto is None else crypto == "ACTIVE",
            order_types=None,
            raw=json.loads(json.dumps(acct, default=str)),
        )

    async def health_check(self) -> None:
        resp = await self._request("GET", "/v2/account")
        if resp.status_code >= 300:
            raise self._fail(resp)
