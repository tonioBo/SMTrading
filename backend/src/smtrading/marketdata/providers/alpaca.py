import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import websockets

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider, StreamRefused, StreamSymbolLimit
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Bar, Capability, DayChange, NewsItem, Quote, Timeframe
from smtrading.marketdata.wscoalesce import PING_TIMEOUT_S, coalesced

BASE_URL = "https://data.alpaca.markets"
WS_BASE = "wss://stream.data.alpaca.markets"
DEFAULT_FEED = "iex"
BARS_PAGE_LIMIT = 10000
NEWS_MAX_LIMIT = 50
INVALID_KEY = "alpaca: chiave non valida"
# the free data plan allows one socket per key: a 406 means another session (another
# client with the same key, or our own dropped one Alpaca has not timed out yet) holds it
CONNECTION_LIMIT = 406
# the free data plan also caps the symbols of a subscription: 405 «symbol limit exceeded»
SYMBOL_LIMIT = 405
CONNECTION_LIMIT_RETRY_S = 180

TIMEFRAMES = {
    Timeframe.m1: "1Min",
    Timeframe.m5: "5Min",
    Timeframe.m15: "15Min",
    Timeframe.h1: "1Hour",
    Timeframe.d1: "1Day",
    Timeframe.w1: "1Week",
}


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _price(value: object) -> Decimal | None:
    """Alpaca reports a missing side of the book as 0."""
    d = _dec(value)
    return d if d else None


def _ts(raw: object) -> datetime | None:
    """None for a missing or garbage time: never now(), which would defeat STALE_QUOTE."""
    if isinstance(raw, str):
        try:  # RFC-3339 with nanoseconds: fromisoformat truncates past microseconds
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            pass
    return None


def _is_crypto(provider_symbol: str) -> bool:
    return "/" in provider_symbol


def parse_alpaca_ws(
    msg_list: list, reverse_map: dict[str, str] | None = None, state: dict[str, Quote] | None = None
) -> list[Quote]:
    """Pure parser for a WS frame (a JSON array) of quote (T == "q") and trade (T == "t")
    messages, merged per symbol: a quote sets bid/ask keeping the last known trade price, a
    trade sets last keeping the last known bid/ask; ts is the newest message time seen.
    state: canonical -> last emitted Quote, kept across frames by the caller (one per socket).
    reverse_map: Alpaca symbol -> canonical; unmapped symbols fall back to "BTC/USD" -> "BTC-USD"."""
    if state is None:
        state = {}
    quotes: list[Quote] = []
    for msg in msg_list:
        if not isinstance(msg, dict) or msg.get("T") not in ("q", "t"):
            continue
        symbol = msg.get("S")
        if not isinstance(symbol, str):
            continue
        if reverse_map is not None:
            canonical = reverse_map.get(symbol)
            if canonical is None:
                continue
        else:
            canonical = symbol.replace("/", "-")
        ts = _ts(msg.get("t"))
        if ts is None:
            continue
        prev = state.get(canonical)
        if prev is not None and prev.ts > ts:
            ts = prev.ts  # a late message never moves the quote time backwards
        if msg["T"] == "q":
            bid, ask, last = _price(msg.get("bp")), _price(msg.get("ap")), prev.last if prev else None
        else:
            last = _price(msg.get("p"))
            if last is None:
                continue
            bid, ask = (prev.bid, prev.ask) if prev else (None, None)
        quote = Quote(symbol=canonical, bid=bid, ask=ask, last=last, ts=ts, provider="alpaca")
        state[canonical] = quote
        quotes.append(quote)
    return quotes


@register
class AlpacaProvider(DataProvider):
    # free plan: 30 symbols per subscription (quotes and trades both subscribed)
    stream_symbol_limit = 30
    name = "alpaca"
    capabilities = frozenset({Capability.quote, Capability.bars, Capability.stream, Capability.news})
    asset_classes = frozenset({AssetClass.us_equity, AssetClass.crypto})
    requires_key = True
    config_fields = ("key_id", "secret_key")  # the Alpaca account's API keys
    default_rate_limit_per_min = 200
    # its feed (fiscal.ai) tags "QQQ" on Questcorp Mining (CSE), "T" on TELUS (TSX), ...
    news_tags_unverified = True

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class == AssetClass.us_equity:
            return symbol
        if asset_class == AssetClass.crypto:
            base, sep, quote = symbol.partition("-")
            if sep and base and quote:
                return f"{base}/{quote}"
        raise self._unsupported(f"symbol {symbol}")

    @property
    def feed(self) -> str:
        return self.config.get("feed") or DEFAULT_FEED

    def _credentials(self) -> tuple[str, str]:
        key_id, secret = self.config.get("key_id"), self.config.get("secret_key")
        if not key_id or not secret:
            raise self._unsupported("request without API keys")
        return key_id, secret

    async def _get(self, path: str, params: dict) -> dict:
        key_id, secret = self._credentials()
        resp = await self.http.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(INVALID_KEY)
        resp.raise_for_status()
        return json.loads(resp.text, parse_float=Decimal)

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        stocks = [(c, ps) for c, ps in items if not _is_crypto(ps)]
        crypto = [(c, ps) for c, ps in items if _is_crypto(ps)]
        out: dict[str, Quote] = {}
        if stocks:
            params = {"symbols": ",".join(ps for _, ps in stocks), "feed": self.feed}
            quotes = (await self._get("/v2/stocks/quotes/latest", params))["quotes"]
            trades = (await self._get("/v2/stocks/trades/latest", params))["trades"]
            out.update(self._build(stocks, quotes, trades))
        if crypto:
            params = {"symbols": ",".join(ps for _, ps in crypto)}
            quotes = (await self._get("/v1beta3/crypto/us/latest/quotes", params))["quotes"]
            trades = (await self._get("/v1beta3/crypto/us/latest/trades", params))["trades"]
            out.update(self._build(crypto, quotes, trades))
        return out

    day_change_chunk = 200  # symbols per snapshots request (the URL stays short)
    day_change_cost = 1

    async def get_day_changes(self, items: list[tuple[str, str]]) -> dict[str, DayChange]:
        """One snapshots request per asset type: the latest session's daily bar close
        against the previous daily bar's close (both from the same feed)."""
        stocks = [(c, ps) for c, ps in items if not _is_crypto(ps)]
        crypto = [(c, ps) for c, ps in items if _is_crypto(ps)]
        out: dict[str, DayChange] = {}
        if stocks:
            params = {"symbols": ",".join(ps for _, ps in stocks), "feed": self.feed}
            out.update(self._changes(stocks, await self._get("/v2/stocks/snapshots", params)))
        if crypto:
            params = {"symbols": ",".join(ps for _, ps in crypto)}
            data = await self._get("/v1beta3/crypto/us/snapshots", params)
            out.update(self._changes(crypto, data.get("snapshots") or {}))
        return out

    def _changes(self, items: list[tuple[str, str]], snaps: dict) -> dict[str, DayChange]:
        out: dict[str, DayChange] = {}
        for canonical, ps in items:
            snap = snaps.get(ps)
            if not isinstance(snap, dict):
                continue
            day, prev = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
            last, prev_close = _price(day.get("c")), _price(prev.get("c"))
            ts = _ts((snap.get("latestTrade") or {}).get("t")) or _ts(day.get("t"))
            if last is None or prev_close is None or ts is None:
                continue
            out[canonical] = DayChange(canonical, last, prev_close, ts, self.name)
        return out

    def _build(self, items: list[tuple[str, str]], quotes: dict, trades: dict) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for canonical, ps in items:
            q, t = quotes.get(ps), trades.get(ps)
            if not q and not t:
                continue
            q, t = q or {}, t or {}
            ts = _ts(q.get("t") or t.get("t"))
            if ts is None:
                continue
            out[canonical] = Quote(
                symbol=canonical,
                bid=_price(q.get("bp")),
                ask=_price(q.get("ap")),
                last=_price(t.get("p")),
                ts=ts,
                provider=self.name,
            )
        return out

    async def get_bars(
        self, symbol: str, provider_symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        crypto = _is_crypto(provider_symbol)
        path = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
        params: dict = {
            "symbols": provider_symbol,
            "timeframe": TIMEFRAMES[timeframe],
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "limit": BARS_PAGE_LIMIT,
        }
        if not crypto:
            params["feed"] = self.feed
        bars: list[Bar] = []
        while True:
            data = await self._get(path, params)
            for r in (data.get("bars") or {}).get(provider_symbol) or []:
                if _ts(r["t"]) is None:
                    continue
                bars.append(
                    Bar(
                        ts=_ts(r["t"]),
                        open=_dec(r["o"]),
                        high=_dec(r["h"]),
                        low=_dec(r["l"]),
                        close=_dec(r["c"]),
                        volume=_dec(r["v"]) or Decimal(0),
                    )
                )
            token = data.get("next_page_token")
            if not token:
                return bars
            params = {**params, "page_token": token}

    async def get_news(self, symbols: list[tuple[str, str]], limit: int) -> list[NewsItem]:
        # news symbols have no slash: "BTC/USD" is "BTCUSD"
        reverse = {ps.replace("/", ""): canonical for canonical, ps in symbols}
        data = await self._get(
            "/v1beta1/news",
            {"symbols": ",".join(reverse), "limit": max(1, min(limit, NEWS_MAX_LIMIT))},
        )
        items: list[NewsItem] = []
        for n in data.get("news") or []:
            if not n.get("headline") or _ts(n.get("created_at")) is None or n.get("id") is None:
                continue
            items.append(
                NewsItem(
                    id=str(n["id"]),
                    ts=_ts(n["created_at"]),
                    headline=n["headline"],
                    summary=n.get("summary") or None,
                    url=n.get("url"),
                    source=n.get("source") or "alpaca",
                    symbols=tuple(reverse[s] for s in n.get("symbols") or [] if s in reverse),
                )
            )
        return sorted(items, key=lambda n: n.ts, reverse=True)[:limit]

    async def stream(self, items: list[tuple[str, str]]) -> AsyncIterator[Quote]:
        key_id, secret = self._credentials()
        crypto = bool(items) and _is_crypto(items[0][1])
        reverse = {ps: c for c, ps in items if _is_crypto(ps) == crypto}
        url = self.config.get("ws_url") or (
            f"{WS_BASE}/v1beta3/crypto/us" if crypto else f"{WS_BASE}/v2/{self.feed}"
        )
        auth = {"action": "auth", "key": key_id, "secret": secret}
        # trades too (free IEX and crypto feeds): quotes alone carry no last price
        subscribe = {"action": "subscribe", "quotes": list(reverse), "trades": list(reverse)}
        merged: dict[str, Quote] = {}

        async def handle(raw) -> list[Quote]:
            frame = json.loads(raw, parse_float=Decimal)
            msgs = frame if isinstance(frame, list) else [frame]
            for msg in msgs:
                kind = msg.get("T")
                if kind == "error":
                    text = f"alpaca stream error {msg.get('code')}: {msg.get('msg')}"
                    if msg.get("code") == SYMBOL_LIMIT:
                        raise StreamSymbolLimit(text)
                    if msg.get("code") == CONNECTION_LIMIT:
                        raise StreamRefused(
                            text + " (one socket per key: another session holds it)",
                            retry_after_s=CONNECTION_LIMIT_RETRY_S)
                    raise ConnectionError(text)
                if kind == "success" and msg.get("msg") == "connected":
                    await ws.send(json.dumps(auth))
                elif kind == "success" and msg.get("msg") == "authenticated":
                    await ws.send(json.dumps(subscribe))
            return parse_alpaca_ws(msgs, reverse, merged)

        try:
            async with websockets.connect(url, ping_timeout=PING_TIMEOUT_S) as ws:
                async for quote in coalesced(ws, handle):
                    yield quote
        except (StreamRefused, StreamSymbolLimit):
            raise
        except (TimeoutError, websockets.WebSocketException, OSError) as exc:
            raise ConnectionError(f"alpaca websocket: {exc}") from exc
        raise ConnectionError("alpaca websocket closed")

    async def health_check(self) -> None:
        await self._get("/v2/stocks/quotes/latest", {"symbols": "SPY", "feed": self.feed})
