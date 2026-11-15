"""Revolut X (data only): signed REST ticker reads. No order endpoint is, or must be, implemented.

Docs: https://developer.revolut.com/docs/x-api/revolut-x-crypto-exchange-rest-api
Auth: X-Revx-API-Key, X-Revx-Timestamp (unix ms), X-Revx-Signature = base64(Ed25519(
timestamp + METHOD + path + query + body)); path starts at /api (e.g. /api/1.0/tickers).
"""

import base64
import json
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote as urlquote

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Capability, Quote

BASE_URL = "https://revx.revolut.com"
TICKERS_PATH = "/api/1.0/tickers"


def sign_request(
    private_key_pem: str, timestamp_ms: int | str, method: str, path: str, query: str = "", body: str = ""
) -> str:
    """Base64 Ed25519 signature of timestamp + METHOD + path + query (no "?") + body."""
    key = load_pem_private_key(private_key_pem.encode(), password=None)
    message = f"{timestamp_ms}{method.upper()}{path}{query.lstrip('?')}{body}".encode()
    return base64.b64encode(key.sign(message)).decode()  # type: ignore[call-arg]


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def parse_tickers(payload: dict, wanted: dict[str, str], provider: str = "revolutx") -> dict[str, Quote]:
    """wanted: response symbol ("BTC/USD") -> canonical symbol. ts = metadata.timestamp (ms)."""
    raw_ts = (payload.get("metadata") or {}).get("timestamp")
    try:  # a missing or garbage time skips the batch: never now() (STALE_QUOTE)
        ts = datetime.fromtimestamp(int(raw_ts) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return {}
    out: dict[str, Quote] = {}
    for entry in payload.get("data") or []:
        canonical = wanted.get(entry.get("symbol", ""))
        last = _dec(entry.get("last_price"))
        if canonical is None or last is None or last <= 0:
            continue
        out[canonical] = Quote(
            symbol=canonical, bid=_dec(entry.get("bid")), ask=_dec(entry.get("ask")), last=last,
            ts=ts, provider=provider,
        )
    return out


@register
class RevolutXProvider(DataProvider):
    name = "revolutx"
    capabilities = frozenset({Capability.quote})
    asset_classes = frozenset({AssetClass.crypto})
    requires_key = True
    config_fields = ("api_key", "private_key_pem")
    default_rate_limit_per_min = 60

    def _credentials(self) -> tuple[str, str]:
        api_key, pem = self.config.get("api_key"), self.config.get("private_key_pem")
        if not api_key or not pem:
            raise self._unsupported("request without api_key/private_key_pem")
        return api_key, pem

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        base, sep, quote = symbol.partition("-")
        if asset_class != AssetClass.crypto or not sep or not base or not quote:
            raise self._unsupported(f"symbol {symbol}")
        return symbol  # request paths/queries use "BTC-USD"; responses use "BTC/USD"

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        api_key, pem = self._credentials()
        query = "symbols=" + urlquote(",".join(ps for _, ps in items), safe="")
        timestamp = str(int(time.time() * 1000))
        headers = {
            "X-Revx-API-Key": api_key,
            "X-Revx-Timestamp": timestamp,
            "X-Revx-Signature": sign_request(pem, timestamp, "GET", TICKERS_PATH, query),
        }
        resp = await self.http.get(f"{BASE_URL}{TICKERS_PATH}?{query}", headers=headers)
        resp.raise_for_status()
        payload = json.loads(resp.text, parse_float=Decimal)
        return parse_tickers(payload, {ps.replace("-", "/"): canonical for canonical, ps in items})

    async def health_check(self) -> None:
        await self.get_quotes([("BTC-USD", "BTC-USD")])
