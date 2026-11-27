import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import websockets

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, Quote, Timeframe
from smtrading.marketdata.wscoalesce import PING_TIMEOUT_S, coalesced

BASE_URL = "https://api.binance.com/api/v3"
WS_URL = "wss://stream.binance.com:9443/stream"

KLINES_LIMIT = 1000

INTERVALS = {
    Timeframe.m1: "1m",
    Timeframe.m5: "5m",
    Timeframe.m15: "15m",
    Timeframe.h1: "1h",
    Timeframe.d1: "1d",
    Timeframe.w1: "1w",
}


def parse_binance_ws(msg: dict, reverse_map: dict[str, str]) -> list[Quote]:
    """Pure parser for combined-stream bookTicker messages. reverse_map: "BTCUSDT" -> canonical.

    bookTicker has no event time and no last price: ts is "now" (the stream is live), last=None.
    """
    data = msg.get("data") or {}
    canonical = reverse_map.get(data.get("s", ""))
    if canonical is None or "b" not in data or "a" not in data:
        return []
    return [
        Quote(
            symbol=canonical,
            bid=Decimal(str(data["b"])),
            ask=Decimal(str(data["a"])),
            last=None,
            ts=datetime.now(UTC),
            provider="binance",
        )
    ]


@register
class BinanceProvider(DataProvider):
    """Binance spot. USD is approximated with USDT (BTC-USD -> BTCUSDT): the prices differ
    slightly from true USD, which is why Kraken has priority for crypto."""

    name = "binance"
    capabilities = frozenset({Capability.quote, Capability.bars, Capability.stream})
    asset_classes = frozenset({AssetClass.crypto})
    requires_key = False
    default_rate_limit_per_min = 600

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        base, sep, quote = symbol.partition("-")
        if asset_class != AssetClass.crypto or not sep or not base or not quote:
            raise self._unsupported(f"symbol {symbol}")
        return f"{base}{'USDT' if quote == 'USD' else quote}"

    async def _get(self, path: str, params: dict) -> list | dict:
        # HTTP errors (incl. 451 geo-block) propagate so the router falls back.
        resp = await self.http.get(f"{BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return json.loads(resp.text, parse_float=Decimal)

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        symbols = json.dumps([ps for _, ps in items], separators=(",", ":"))
        books = await self._get("/ticker/bookTicker", {"symbols": symbols})
        prices = await self._get("/ticker/price", {"symbols": symbols})
        book_by = {b["symbol"]: b for b in books}
        price_by = {p["symbol"]: p for p in prices}
        now = datetime.now(UTC)  # bookTicker carries no timestamp; the call is live
        out: dict[str, Quote] = {}
        for canonical, ps in items:
            book = book_by.get(ps)
            if not book:
                continue
            price = price_by.get(ps)
            out[canonical] = Quote(
                symbol=canonical,
                bid=Decimal(book["bidPrice"]),
                ask=Decimal(book["askPrice"]),
                last=Decimal(price["price"]) if price else None,
                ts=now,
                provider=self.name,
            )
        return out

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        # klines return at most 1000 rows per call: page forward until `end` or a short page.
        out: list[Bar] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        while cursor <= end_ms:
            rows = await self._get(
                "/klines",
                {
                    "symbol": provider_symbol,
                    "interval": INTERVALS[timeframe],
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": KLINES_LIMIT,
                },
            )
            out.extend(
                Bar(
                    ts=datetime.fromtimestamp(int(r[0]) / 1000, UTC),
                    open=Decimal(r[1]),
                    high=Decimal(r[2]),
                    low=Decimal(r[3]),
                    close=Decimal(r[4]),
                    volume=Decimal(r[5]),
                )
                for r in rows
            )
            if len(rows) < KLINES_LIMIT:
                break
            cursor = int(rows[-1][0]) + 1
        return out

    async def stream(
        self, items: list[tuple[str, str]], *, bars: frozenset[str] = frozenset()
    ) -> AsyncIterator[Quote]:  # no minute bars: `bars` ignored
        reverse = {ps: canonical for canonical, ps in items}
        streams = "/".join(f"{ps.lower()}@bookTicker" for ps in reverse)
        url = f"{self.config.get('ws_url', WS_URL)}?streams={streams}"

        async def handle(raw) -> list[Quote]:
            return parse_binance_ws(json.loads(raw), reverse)

        try:
            async with websockets.connect(url, ping_timeout=PING_TIMEOUT_S) as ws:
                async for quote in coalesced(ws, handle):
                    yield quote
        except (TimeoutError, websockets.WebSocketException, OSError) as exc:
            raise ConnectionError(f"binance websocket: {exc}") from exc
        raise ConnectionError("binance websocket closed")

    async def health_check(self) -> None:
        resp = await self.http.get(f"{BASE_URL}/ping")
        resp.raise_for_status()
