import json
from decimal import Decimal, InvalidOperation
from typing import ClassVar

import httpx

from smtrading.brokers.base import BrokerAdapter, BrokerOrder, BrokerStatus
from smtrading.brokers.registry import register
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderType
from smtrading.instruments.models import AssetClass, Instrument

PAPER_URL = "https://paper-api.alpaca.markets"
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


@register
class AlpacaPaperAdapter(BrokerAdapter):
    kind: ClassVar[str] = "alpaca_paper"
    supported_types: ClassVar[frozenset[OrderType]] = frozenset(OrderType)
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(
        {AssetClass.us_equity, AssetClass.crypto}
    )

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        base = str(config.get("base_url") or PAPER_URL).rstrip("/")
        if base != PAPER_URL:
            raise SMTError(ErrorCode.FORBIDDEN, "alpaca_paper: only the paper endpoint is allowed")
        self.base_url = PAPER_URL
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
        return True

    async def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        try:
            return await self.http.request(
                method, self.base_url + path, headers=self._headers, json=json_body
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"alpaca_paper: {type(e).__name__}") from None

    @staticmethod
    def _parse(resp: httpx.Response):
        return json.loads(resp.content, parse_float=Decimal)

    def _fail(self, resp: httpx.Response) -> RuntimeError:
        return RuntimeError(f"alpaca_paper: HTTP {resp.status_code}: {_error_message(resp)}")

    @staticmethod
    def _to_broker_order(data: dict) -> BrokerOrder:
        status = _STATUS.get(str(data.get("status")), "accepted")
        return BrokerOrder(
            broker_order_id=str(data["id"]),
            status=status,
            filled_qty=_dec(data.get("filled_qty")) or Decimal(0),
            avg_fill_price=_dec(data.get("filled_avg_price")),
            message=None,
            raw=data,
        )

    def _payload(self, order: Order, instrument: Instrument) -> dict:
        crypto = instrument.asset_class is AssetClass.crypto
        body: dict = {
            "symbol": to_alpaca_symbol(order.symbol, instrument.asset_class),
            "side": str(order.side),
            "type": "stop_limit" if order.type is OrderType.stop_limit else str(order.type),
            "time_in_force": "gtc" if crypto else str(order.tif),
            "client_order_id": str(order.id),
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
        resp = await self._request("POST", "/v2/orders", json_body=self._payload(order, instrument))
        if resp.status_code in _REJECT_HTTP:
            return BrokerOrder("", "rejected", Decimal(0), None, _error_message(resp), {})
        if resp.status_code >= 300:
            raise self._fail(resp)
        return self._to_broker_order(self._parse(resp))

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

    async def health_check(self) -> None:
        resp = await self._request("GET", "/v2/account")
        if resp.status_code >= 300:
            raise self._fail(resp)
