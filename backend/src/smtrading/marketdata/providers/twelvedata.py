import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx
import websockets

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, Quote, Timeframe

BASE_URL = "https://api.twelvedata.com"
WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"
HEARTBEAT_SECONDS = 10

INTERVALS = {
    Timeframe.m1: "1min",
    Timeframe.m5: "5min",
    Timeframe.m15: "15min",
    Timeframe.h1: "1h",
    Timeframe.d1: "1day",
    Timeframe.w1: "1week",
}
# canonical Yahoo-style suffix -> Twelve Data exchange name (/exchanges: XMIL "MTA",
# XETR "XETR", XPAR "Euronext")
EXCHANGES = {"MI": "MTA", "DE": "XETR", "PA": "Euronext"}
MAX_OUTPUT = 5000


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _from_epoch(raw: object) -> datetime | None:
    """None for a missing or garbage timestamp: never now(), which would defeat STALE_QUOTE."""
    try:
        return datetime.fromtimestamp(int(raw), UTC) if raw else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _raise_if_error(data: object) -> None:
    """Twelve Data reports failures with HTTP 200 and {"status": "error", "code", "message"}."""
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"twelvedata error {data.get('code')}: {data.get('message')}")


def parse_quote(canonical: str, entry: dict, provider: str = "twelvedata") -> Quote | None:
    """/quote entry. bid/ask are not part of the documented response; `close` is the last price."""
    last = _dec(entry.get("close"))
    ts = _from_epoch(entry.get("last_quote_at") or entry.get("timestamp"))
    if last is None or last <= 0 or ts is None:
        return None
    return Quote(
        symbol=canonical,
        bid=_dec(entry.get("bid")),
        ask=_dec(entry.get("ask")),
        last=last,
        ts=ts,
        provider=provider,
    )


def parse_twelvedata_ws(msg: dict, reverse_map: dict[str, str]) -> list[Quote]:
    """Pure parser for WS price events. reverse_map: provider symbol (or bare ticker) -> canonical."""
    if msg.get("event") != "price":
        return []
    canonical = reverse_map.get(str(msg.get("symbol", "")))
    price = _dec(msg.get("price"))
    ts = _from_epoch(msg.get("timestamp"))
    if canonical is None or price is None or price <= 0 or ts is None:
        return []
    return [
        Quote(
            symbol=canonical,
            bid=_dec(msg.get("bid")),
            ask=_dec(msg.get("ask")),
            last=price,
            ts=ts,
            provider="twelvedata",
        )
    ]


@register
class TwelveDataProvider(DataProvider):
    name = "twelvedata"
    capabilities = frozenset({Capability.quote, Capability.bars, Capability.stream, Capability.fx})
    asset_classes = frozenset({AssetClass.eu_equity, AssetClass.forex, AssetClass.us_equity})
    requires_key = True
    default_rate_limit_per_min = 8
    default_daily_budget = 800  # free plan: 800 credits/day, reset 00:00 UTC

    def _key(self) -> str:
        key = self.config.get("api_key")
        if not key:
            raise self._unsupported("request without api_key")
        return key

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class == AssetClass.forex:
            if len(symbol) != 6 or not symbol.isalpha():
                raise self._unsupported(f"symbol {symbol}")
            return f"{symbol[:3]}/{symbol[3:]}"
        if asset_class == AssetClass.eu_equity:
            ticker, dot, suffix = symbol.rpartition(".")
            if not dot or not ticker or suffix not in EXCHANGES:
                raise self._unsupported(f"symbol {symbol}")
            return f"{ticker}:{EXCHANGES[suffix]}"
        if asset_class == AssetClass.us_equity:
            return symbol
        raise self._unsupported(f"symbol {symbol}")

    @staticmethod
    def _rest_params(provider_symbol: str) -> dict:
        """REST takes the exchange as its own parameter; WS accepts the "TICKER:EXCHANGE" form."""
        ticker, sep, exchange = provider_symbol.partition(":")
        return {"symbol": ticker, "exchange": exchange} if sep else {"symbol": provider_symbol}

    async def _get(self, path: str, params: dict) -> dict:
        try:
            resp = await self.http.get(f"{BASE_URL}{path}", params={**params, "apikey": self._key()})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:  # message embeds the URL, which carries the apikey
            raise RuntimeError(f"twelvedata: HTTP {exc.response.status_code}") from None
        except httpx.RequestError as exc:
            raise RuntimeError(f"twelvedata: request failed: {type(exc).__name__}") from None
        data = json.loads(resp.text, parse_float=Decimal)
        _raise_if_error(data)
        return data

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        plain = [(c, ps) for c, ps in items if ":" not in ps]
        qualified = [(c, ps) for c, ps in items if ":" in ps]
        if plain:
            data = await self._get("/quote", {"symbol": ",".join(ps for _, ps in plain)})
            # one symbol -> flat entry; several -> {symbol: entry}
            by_symbol = {plain[0][1]: data} if len(plain) == 1 else data
            for canonical, ps in plain:
                entry = by_symbol.get(ps)
                if isinstance(entry, dict):
                    _raise_if_error(entry)
                    quote = parse_quote(canonical, entry)
                    if quote:
                        out[canonical] = quote
        for canonical, ps in qualified:
            quote = parse_quote(canonical, await self._get("/quote", self._rest_params(ps)))
            if quote:
                out[canonical] = quote
        return out

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        fmt = "%Y-%m-%dT%H:%M:%S"
        data = await self._get(
            "/time_series",
            {
                **self._rest_params(provider_symbol),
                "interval": INTERVALS[timeframe],
                "start_date": start.astimezone(UTC).strftime(fmt),
                "end_date": end.astimezone(UTC).strftime(fmt),
                "timezone": "UTC",
                "order": "asc",
                "outputsize": MAX_OUTPUT,
            },
        )
        bars = []
        for row in data.get("values") or []:
            raw = row["datetime"]
            ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S" if " " in raw else "%Y-%m-%d")
            bars.append(
                Bar(
                    ts=ts.replace(tzinfo=UTC),
                    open=_dec(row["open"]),
                    high=_dec(row["high"]),
                    low=_dec(row["low"]),
                    close=_dec(row["close"]),
                    volume=_dec(row.get("volume")) or Decimal(0),
                )
            )
        return bars

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        data = await self._get("/exchange_rate", {"symbol": f"{base}/{quote}"})
        rate = _dec(data.get("rate"))
        if rate is None:
            raise RuntimeError(f"twelvedata: no exchange rate for {base}/{quote}")
        return rate

    async def stream(
        self, items: list[tuple[str, str]], *, bars: frozenset[str] = frozenset()
    ) -> AsyncIterator[Quote]:  # no minute bars: `bars` ignored
        key = self._key()
        reverse: dict[str, str] = {}
        for canonical, ps in items:
            reverse[ps] = canonical
            reverse.setdefault(ps.partition(":")[0], canonical)  # events may omit the exchange
        url = f"{self.config.get('ws_url', WS_URL)}?apikey={key}"
        subscribe = {"action": "subscribe", "params": {"symbols": ",".join(ps for _, ps in items)}}
        beat: asyncio.Task | None = None

        async def heartbeat(ws) -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                await ws.send(json.dumps({"action": "heartbeat"}))

        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(subscribe))
                beat = asyncio.create_task(heartbeat(ws))
                async for raw in ws:
                    msg = json.loads(raw, parse_float=Decimal)
                    if msg.get("event") == "subscribe-status" and not msg.get("success"):
                        raise ConnectionError(f"twelvedata subscribe rejected: {msg.get('fails')}")
                    for quote in parse_twelvedata_ws(msg, reverse):
                        yield quote
        except ConnectionError:
            raise
        except (TimeoutError, websockets.WebSocketException, OSError) as exc:
            raise ConnectionError(f"twelvedata websocket: {type(exc).__name__}") from None  # message may embed the URL
        finally:
            if beat:
                beat.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await beat
        raise ConnectionError("twelvedata websocket closed")

    async def health_check(self) -> None:
        await self._get("/api_usage", {})
