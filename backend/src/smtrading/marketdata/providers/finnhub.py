import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote as urlquote

import httpx
import websockets

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider, ProviderUnsupported
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import CalendarEvent, Capability, Fundamentals, NewsItem, Quote
from smtrading.marketdata.wscoalesce import PING_TIMEOUT_S, coalesced

BASE_URL = "https://finnhub.io/api/v1"
WS_URL = "wss://ws.finnhub.io"
NEWS_LOOKBACK_DAYS = 7
INVALID_KEY = "finnhub: chiave non valida"
NOT_IN_PLAN = "finnhub: non incluso nel piano Finnhub"
# 403 bodies meaning "valid key, but this market/endpoint is outside your plan" (free plan on EU
# symbols answers {"error": "You don't have access to this resource."}); whole phrases only:
# any other 403 («Access denied», «explanation») is a key problem
_PLAN_403 = re.compile(r"(?i)\bdon['\u2019]?t have access\b|\b(?:premium|subscription|plan)\b")
MILLION = Decimal(1_000_000)


def _dec(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _from_unix(seconds: object) -> datetime | None:
    """None for a missing/zero/garbage time: never now(), which would defeat STALE_QUOTE."""
    d = _dec(seconds)
    try:
        return datetime.fromtimestamp(float(d), UTC) if d and d > 0 else None
    except (OverflowError, OSError, ValueError):
        return None


def parse_finnhub_ws(msg: dict, reverse_map: dict[str, str]) -> list[Quote]:
    """Pure parser for a WS message. Only "trade" events yield quotes (last = price p, t in ms)."""
    if not isinstance(msg, dict) or msg.get("type") != "trade":
        return []
    quotes: list[Quote] = []
    for trade in msg.get("data") or []:
        canonical = reverse_map.get(trade.get("s", ""))
        price = _dec(trade.get("p"))
        if canonical is None or not price:  # None or 0 = missing, as in REST
            continue
        t = _dec(trade.get("t"))
        ts = _from_unix(t / 1000) if t else None
        if ts is None:
            continue
        quotes.append(Quote(symbol=canonical, bid=None, ask=None, last=price, ts=ts, provider="finnhub"))
    return quotes


@register
class FinnhubProvider(DataProvider):
    name = "finnhub"
    capabilities = frozenset(
        {Capability.quote, Capability.stream, Capability.news, Capability.fundamentals, Capability.calendar}
    )
    asset_classes = frozenset({AssetClass.us_equity, AssetClass.eu_equity})
    requires_key = True
    default_rate_limit_per_min = 60

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        # US tickers are bare; other exchanges use the exchange suffix ("ENI.MI"), which is
        # already the canonical form.
        if asset_class in (AssetClass.us_equity, AssetClass.eu_equity):
            return symbol
        raise self._unsupported(f"symbol {symbol}")

    def _token(self) -> str:
        token = self.config.get("api_key")
        if not token:
            raise self._unsupported("request without API key")
        return token

    async def _get(self, path: str, params: dict) -> dict | list:
        try:
            resp = await self.http.get(f"{BASE_URL}{path}", params={**params, "token": self._token()})
            if resp.status_code == 403 and _PLAN_403.search(resp.text or ""):
                # not a key problem: skip the provider like an unsupported symbol
                raise ProviderUnsupported(NOT_IN_PLAN)
            if resp.status_code in (401, 403):
                raise RuntimeError(INVALID_KEY)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:  # message embeds the URL, which carries the token
            raise RuntimeError(f"finnhub: HTTP {exc.response.status_code}") from None
        except httpx.RequestError as exc:
            raise RuntimeError(f"finnhub: request failed: {type(exc).__name__}") from None
        return json.loads(resp.text, parse_float=Decimal)

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for canonical, ps in items:
            data = await self._get("/quote", {"symbol": ps})
            last = _dec(data.get("c"))
            ts = _from_unix(data.get("t"))
            # all zeros: unknown symbol, or a market the plan does not cover
            if not last or last <= 0 or ts is None:
                continue
            out[canonical] = Quote(
                symbol=canonical, bid=None, ask=None, last=last, ts=ts, provider=self.name,
            )
        return out

    async def get_news(self, symbols: list[tuple[str, str]], limit: int) -> list[NewsItem]:
        today = datetime.now(UTC).date()
        window = {"from": (today - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat(), "to": today.isoformat()}
        items: dict[str, NewsItem] = {}
        for canonical, ps in symbols:
            for n in await self._get("/company-news", {"symbol": ps, **window}):
                if n.get("id") is None or not n.get("headline") or not n.get("datetime"):
                    continue
                nid = str(n["id"])
                ts = _from_unix(n["datetime"])
                if ts is None:
                    continue
                prev = items.get(nid)
                if prev is not None:
                    if canonical not in prev.symbols:
                        items[nid] = NewsItem(**{**prev.__dict__, "symbols": (*prev.symbols, canonical)})
                    continue
                items[nid] = NewsItem(
                    id=nid,
                    ts=ts,
                    headline=n["headline"],
                    summary=n.get("summary") or None,
                    url=n.get("url") or None,
                    source=n.get("source") or "Finnhub",
                    symbols=(canonical,),
                )
        return sorted(items.values(), key=lambda n: n.ts, reverse=True)[:limit]

    async def get_fundamentals(self, symbol: str, provider_symbol: str) -> Fundamentals:
        profile = await self._get("/stock/profile2", {"symbol": provider_symbol})
        if not profile:  # unknown symbol: Finnhub answers {}
            raise self._unsupported(f"fundamentals for {symbol}")
        metric = (await self._get("/stock/metric", {"symbol": provider_symbol, "metric": "all"})).get(
            "metric"
        ) or {}
        cap = _dec(profile.get("marketCapitalization"))  # reported in millions
        extra = {
            k: str(v)
            for k, v in (
                ("beta", _dec(metric.get("beta"))),
                ("shareOutstanding", _dec(profile.get("shareOutstanding"))),
                ("exchange", profile.get("exchange")),
                ("country", profile.get("country")),
            )
            if v is not None
        }
        return Fundamentals(
            symbol=symbol,
            name=profile.get("name"),
            sector=None,  # Finnhub only classifies by industry
            industry=profile.get("finnhubIndustry"),
            market_cap=cap * MILLION if cap is not None else None,
            pe=_dec(metric.get("peTTM")),
            eps=_dec(metric.get("epsTTM")),
            dividend_yield=_dec(metric.get("dividendYieldIndicatedAnnual")),  # already percent
            high_52w=_dec(metric.get("52WeekHigh")),
            low_52w=_dec(metric.get("52WeekLow")),
            currency=profile.get("currency"),
            extra=extra,
        )

    async def get_calendar(
        self, symbols: list[tuple[str, str]], start: date, end: date
    ) -> list[CalendarEvent]:
        """Earnings only: /stock/dividend is a premium endpoint, so dividend events stay on Yahoo."""
        events: list[CalendarEvent] = []
        for canonical, ps in symbols:
            data = await self._get(
                "/calendar/earnings",
                {"symbol": ps, "from": start.isoformat(), "to": end.isoformat()},
            )
            for r in data.get("earningsCalendar") or []:
                if not r.get("date"):
                    continue
                payload = {
                    key: str(v)
                    for key, v in (
                        ("eps_estimate", _dec(r.get("epsEstimate"))),
                        ("eps_actual", _dec(r.get("epsActual"))),
                        ("revenue_estimate", _dec(r.get("revenueEstimate"))),
                        ("revenue_actual", _dec(r.get("revenueActual"))),
                        ("hour", r.get("hour") or None),
                        ("quarter", r.get("quarter")),
                        ("year", r.get("year")),
                    )
                    if v is not None
                }
                events.append(CalendarEvent(canonical, "earnings", date.fromisoformat(r["date"]), payload))
        return events

    async def stream(self, items: list[tuple[str, str]]) -> AsyncIterator[Quote]:
        token = self._token()
        reverse = {ps: canonical for canonical, ps in items}
        url = self.config.get("ws_url") or f"{WS_URL}?token={urlquote(token)}"

        async def handle(raw) -> list[Quote]:
            msg = json.loads(raw, parse_float=Decimal)
            if msg.get("type") == "error":
                raise ConnectionError(f"finnhub stream error: {msg.get('msg')}")
            return parse_finnhub_ws(msg, reverse)

        try:
            async with websockets.connect(url, ping_timeout=PING_TIMEOUT_S) as ws:
                for ps in reverse:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": ps}))
                async for quote in coalesced(ws, handle):
                    yield quote
        except ConnectionError:
            raise
        except (TimeoutError, websockets.WebSocketException, OSError) as exc:
            raise ConnectionError(f"finnhub websocket: {type(exc).__name__}") from None  # message may embed the URL
        raise ConnectionError("finnhub websocket closed")

    async def health_check(self) -> None:
        await self._get("/quote", {"symbol": "AAPL"})


