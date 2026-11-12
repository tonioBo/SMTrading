import json
from datetime import UTC, datetime
from decimal import Decimal

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, Quote, Timeframe

BASE_URL = "https://api.binance.com/api/v3"

INTERVALS = {
    Timeframe.m1: "1m",
    Timeframe.m5: "5m",
    Timeframe.m15: "15m",
    Timeframe.h1: "1h",
    Timeframe.d1: "1d",
    Timeframe.w1: "1w",
}


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
        rows = await self._get(
            "/klines",
            {
                "symbol": provider_symbol,
                "interval": INTERVALS[timeframe],
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": 1000,
            },
        )
        return [
            Bar(
                ts=datetime.fromtimestamp(int(r[0]) / 1000, UTC),
                open=Decimal(r[1]),
                high=Decimal(r[2]),
                low=Decimal(r[3]),
                close=Decimal(r[4]),
                volume=Decimal(r[5]),
            )
            for r in rows
        ]

    async def health_check(self) -> None:
        resp = await self.http.get(f"{BASE_URL}/ping")
        resp.raise_for_status()
