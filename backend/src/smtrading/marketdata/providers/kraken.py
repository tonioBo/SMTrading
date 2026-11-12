import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, Quote, Timeframe

BASE_URL = "https://api.kraken.com/0/public"

INTERVALS = {
    Timeframe.m1: 1,
    Timeframe.m5: 5,
    Timeframe.m15: 15,
    Timeframe.h1: 60,
    Timeframe.d1: 1440,
    Timeframe.w1: 10080,
}


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@register
class KrakenProvider(DataProvider):
    name = "kraken"
    capabilities = frozenset({Capability.quote, Capability.bars, Capability.stream})
    asset_classes = frozenset({AssetClass.crypto})
    requires_key = False
    default_rate_limit_per_min = 60

    def __init__(self, config: dict, http):
        super().__init__(config, http)
        self._pair_keys: dict[str, str] = {}  # altname ("XBTUSD") -> response key ("XXBTZUSD")

    @staticmethod
    def _split(symbol: str) -> tuple[str, str]:
        base, sep, quote = symbol.partition("-")
        if not sep or not base or not quote:
            raise ValueError(symbol)
        return base, quote

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class != AssetClass.crypto:
            raise self._unsupported(f"symbol {symbol}")
        try:
            base, quote = self._split(symbol)
        except ValueError:
            raise self._unsupported(f"symbol {symbol}") from None
        return f"{'XBT' if base == 'BTC' else base}{quote}"

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self.http.get(f"{BASE_URL}{path}", params=params)
        resp.raise_for_status()
        data = json.loads(resp.text, parse_float=Decimal)
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data["result"]

    async def _resolve_keys(self, pairs: list[str]) -> None:
        missing = [p for p in pairs if p not in self._pair_keys]
        if not missing:
            return
        result = await self._get("/AssetPairs", {"pair": ",".join(missing)})
        for key, info in result.items():
            self._pair_keys[info.get("altname", key)] = key

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        pairs = [ps for _, ps in items]
        await self._resolve_keys(pairs)
        result = await self._get("/Ticker", {"pair": ",".join(pairs)})
        now = datetime.now(UTC)  # Ticker carries no timestamp; the call is live
        out: dict[str, Quote] = {}
        for canonical, pair in items:
            entry = result.get(self._pair_keys.get(pair, pair))
            if not entry:
                continue
            out[canonical] = Quote(
                symbol=canonical,
                bid=_dec(entry["b"][0]),
                ask=_dec(entry["a"][0]),
                last=_dec(entry["c"][0]),
                ts=now,
                provider=self.name,
            )
        return out

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        result = await self._get(
            "/OHLC",
            {
                "pair": provider_symbol,
                "interval": INTERVALS[timeframe],
                "since": int(start.timestamp()),
            },
        )
        rows = next((v for k, v in result.items() if k != "last"), [])
        bars = [
            Bar(
                ts=datetime.fromtimestamp(int(r[0]), UTC),
                open=_dec(r[1]),
                high=_dec(r[2]),
                low=_dec(r[3]),
                close=_dec(r[4]),
                volume=_dec(r[6]),
            )
            for r in rows
        ]
        return [b for b in bars if b.ts <= end]

    async def health_check(self) -> None:
        await self._get("/SystemStatus", {})
