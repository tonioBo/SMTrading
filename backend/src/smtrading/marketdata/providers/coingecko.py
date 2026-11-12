import json
from datetime import UTC, datetime
from decimal import Decimal

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Capability, Fundamentals, InstrumentHit, Quote

BASE_URL = "https://api.coingecko.com/api/v3"

DEFAULT_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
}


def _dec(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@register
class CoinGeckoProvider(DataProvider):
    name = "coingecko"
    capabilities = frozenset({Capability.quote, Capability.search, Capability.fundamentals})
    asset_classes = frozenset({AssetClass.crypto})
    requires_key = False
    default_rate_limit_per_min = 30

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class != AssetClass.crypto:
            raise self._unsupported(f"symbol {symbol}")
        base, sep, quote = symbol.partition("-")
        coin_id = DEFAULT_IDS.get(base)
        if not sep or coin_id is None or not quote:
            raise self._unsupported(f"symbol {symbol}")
        return f"{coin_id}:{quote.lower()}"

    # Provider symbols are "<coin id>:<vs currency>"; a bare coin id means usd.
    @staticmethod
    def _split(provider_symbol: str) -> tuple[str, str]:
        coin_id, _, vs = provider_symbol.partition(":")
        return coin_id, vs or "usd"

    def _headers(self) -> dict[str, str]:
        key = self.config.get("api_key")
        return {"x-cg-demo-api-key": key} if key else {}

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self.http.get(f"{BASE_URL}{path}", params=params, headers=self._headers())
        resp.raise_for_status()
        return json.loads(resp.text, parse_float=Decimal)

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        parsed = [(canonical, *self._split(ps)) for canonical, ps in items]
        out: dict[str, Quote] = {}
        for vs in sorted({vs for _, _, vs in parsed}):
            group = [(c, i) for c, i, v in parsed if v == vs]
            data = await self._get(
                "/simple/price",
                {
                    "ids": ",".join(sorted({i for _, i in group})),
                    "vs_currencies": vs,
                    "include_last_updated_at": "true",
                },
            )
            for canonical, coin_id in group:
                entry = data.get(coin_id)
                if not entry or entry.get(vs) is None:
                    continue
                updated = entry.get("last_updated_at")
                if updated is None:
                    continue  # never fabricate a timestamp
                ts = datetime.fromtimestamp(int(updated), UTC)
                out[canonical] = Quote(
                    symbol=canonical,
                    bid=None,
                    ask=None,
                    last=_dec(entry[vs]),
                    ts=ts,
                    provider=self.name,
                )
        return out

    async def search(self, query: str, limit: int) -> list[InstrumentHit]:
        data = await self._get("/search", {"query": query})
        hits: dict[str, InstrumentHit] = {}
        for coin in data.get("coins", []):
            if not coin.get("symbol") or not coin.get("name"):
                continue
            symbol = f"{coin['symbol'].upper()}-USD"
            if symbol in hits:
                continue  # keep the first (highest-ranked) hit
            hits[symbol] = InstrumentHit(
                symbol=symbol,
                name=coin["name"],
                asset_class=AssetClass.crypto,
                exchange=None,
                currency="USD",
                provider=self.name,
            )
            if len(hits) >= limit:
                break
        return list(hits.values())

    async def get_fundamentals(self, symbol: str, provider_symbol: str) -> Fundamentals:
        coin_id, vs = self._split(provider_symbol)
        data = await self._get(
            f"/coins/{coin_id}",
            {
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
        )
        md = data.get("market_data") or {}

        def at(key: str) -> Decimal | None:
            return _dec((md.get(key) or {}).get(vs))

        # CoinGecko has no 52-week range: leave it None; 24h and all-time ranges go to extra.
        extra = {k: str(v) for k in ("high_24h", "low_24h", "ath", "atl") if (v := at(k)) is not None}
        return Fundamentals(
            symbol=symbol,
            name=data.get("name"),
            sector=None,
            industry=None,
            market_cap=at("market_cap"),
            pe=None,
            eps=None,
            dividend_yield=None,
            high_52w=None,
            low_52w=None,
            currency=vs.upper(),
            extra=extra,
        )

    async def health_check(self) -> None:
        resp = await self.http.get(f"{BASE_URL}/ping", headers=self._headers())
        resp.raise_for_status()
