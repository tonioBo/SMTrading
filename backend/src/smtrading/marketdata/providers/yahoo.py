import asyncio
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import (
    Bar,
    CalendarEvent,
    Capability,
    Fundamentals,
    InstrumentHit,
    NewsItem,
    Quote,
    ScreenFilter,
    Timeframe,
)

INTERVALS = {
    Timeframe.m1: "1m",
    Timeframe.m5: "5m",
    Timeframe.m15: "15m",
    Timeframe.h1: "60m",
    Timeframe.d1: "1d",
    Timeframe.w1: "1wk",
}

_QUOTE_TYPE_CLASS = {
    "EQUITY": AssetClass.us_equity,
    "ETF": AssetClass.us_equity,
    "CRYPTOCURRENCY": AssetClass.crypto,
    "CURRENCY": AssetClass.forex,
}
_EU_EXCHANGES = frozenset(
    {
        "MIL",
        "GER",
        "FRA",
        "PAR",
        "AMS",
        "LSE",
        "MCE",
        "EBS",
        "STO",
        "CPH",
        "HEL",
        "VIE",
        "BRU",
        "LIS",
        "OSL",
    }
)
_EU_REGIONS = ("it", "de", "fr", "nl", "es", "gb")


def _dec(value: object) -> Decimal | None:
    """Decimal(str(x)); None and NaN/inf become None. Booleans are not numbers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def _positive(value: object) -> Decimal | None:
    d = _dec(value)
    return d if d is not None and d > 0 else None


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def screen_query(f: ScreenFilter) -> dict:
    """Plain-dict form of the yfinance EquityQuery for a ScreenFilter (built into a real
    EquityQuery inside _YF). Node: {"operator": ..., "operands": [...]}; leaves hold a field name
    followed by values, as in yfinance's own dict format."""
    regions = ["us"] if f.asset_class in (None, AssetClass.us_equity) else list(_EU_REGIONS)
    operands: list[dict] = [{"operator": "EQ", "operands": ["region", r]} for r in regions]
    region_node = operands[0] if len(operands) == 1 else {"operator": "OR", "operands": operands}
    conds: list[dict] = [region_node]
    if f.sector:
        conds.append({"operator": "EQ", "operands": ["sector", f.sector]})
    if f.min_market_cap is not None:
        conds.append({"operator": "GT", "operands": ["intradaymarketcap", str(f.min_market_cap)]})
    if f.max_market_cap is not None:
        conds.append({"operator": "LT", "operands": ["intradaymarketcap", str(f.max_market_cap)]})
    if f.min_change_pct is not None:
        conds.append({"operator": "GT", "operands": ["percentchange", str(f.min_change_pct)]})
    if f.max_change_pct is not None:
        conds.append({"operator": "LT", "operands": ["percentchange", str(f.max_change_pct)]})
    if f.min_volume is not None:
        conds.append({"operator": "GT", "operands": ["dayvolume", str(f.min_volume)]})
    if f.max_pe is not None:
        conds.append({"operator": "LT", "operands": ["peratio.lasttwelvemonths", str(f.max_pe)]})
    query = conds[0] if len(conds) == 1 else {"operator": "AND", "operands": conds}
    return {"query": query, "limit": f.limit}


class _YF:
    """Thin synchronous adapter over yfinance. Returns plain JSON-able structures (dicts, lists,
    strings, numbers; dates as ISO strings) so no pandas object leaves this class. Tests replace
    it with a fake; this is the only code that touches yfinance."""

    @staticmethod
    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): _YF._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_YF._clean(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if hasattr(value, "item"):  # numpy scalar
            return _YF._clean(value.item())
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def fast_info(self, sym: str) -> dict:
        import yfinance as yf

        fi = yf.Ticker(sym).fast_info
        return self._clean({k: fi[k] for k in fi})

    def history(self, sym: str, interval: str, start: datetime, end: datetime) -> list[dict]:
        import yfinance as yf

        df = yf.Ticker(sym).history(interval=interval, start=start, end=end, auto_adjust=False)
        rows = []
        for ts, r in df.iterrows():
            rows.append(
                self._clean(
                    {
                        "ts": ts.to_pydatetime().astimezone(UTC),
                        "open": r["Open"],
                        "high": r["High"],
                        "low": r["Low"],
                        "close": r["Close"],
                        "volume": r["Volume"],
                    }
                )
            )
        return rows

    def info(self, sym: str) -> dict:
        import yfinance as yf

        return self._clean(yf.Ticker(sym).info)

    def news(self, sym: str) -> list[dict]:
        import yfinance as yf

        return self._clean(yf.Ticker(sym).news)

    def calendar(self, sym: str) -> dict:
        import yfinance as yf

        return self._clean(yf.Ticker(sym).calendar)

    def dividends(self, sym: str) -> list[dict]:
        import yfinance as yf

        s = yf.Ticker(sym).dividends
        return [{"date": d.date().isoformat(), "amount": self._clean(v)} for d, v in s.items()]

    def splits(self, sym: str) -> list[dict]:
        import yfinance as yf

        s = yf.Ticker(sym).splits
        return [{"date": d.date().isoformat(), "ratio": self._clean(v)} for d, v in s.items()]

    def search(self, q: str) -> list[dict]:
        import yfinance as yf

        return self._clean(yf.Search(q, max_results=20, news_count=0).quotes)

    def screen(self, query: dict) -> list[dict]:
        import yfinance as yf

        def build(node: dict) -> Any:
            ops = [build(o) if isinstance(o, dict) else o for o in node["operands"]]
            # leaf values arrive as strings; numeric comparisons need numbers
            if node["operator"] not in ("AND", "OR", "EQ"):
                ops = [ops[0], float(ops[1])]
            return yf.EquityQuery(node["operator"].lower(), ops)

        res = yf.screen(build(query["query"]), size=query["limit"])
        return self._clean(res.get("quotes", []))


@register
class YahooProvider(DataProvider):
    name = "yahoo"
    capabilities = frozenset(
        {
            Capability.quote,
            Capability.bars,
            Capability.news,
            Capability.fundamentals,
            Capability.calendar,
            Capability.search,
            Capability.screener,
        }
    )
    asset_classes = frozenset(AssetClass)
    requires_key = False
    default_rate_limit_per_min = 60

    def __init__(self, config: dict, http) -> None:
        super().__init__(config, http)
        self._yf = _YF()

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class == AssetClass.forex:
            if len(symbol) != 6 or not symbol.isalpha():
                raise self._unsupported(f"symbol {symbol}")
            return f"{symbol}=X"
        return symbol

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        async def one(canonical: str, ps: str) -> tuple[str, Quote | None]:
            fast = await self._call(self._yf.fast_info, ps)
            last = _dec(fast.get("lastPrice"))
            if last is None:
                return canonical, None
            info = await self._call(self._yf.info, ps)
            # fast_info carries no timestamp: use info["regularMarketTime"] (epoch) if present,
            # otherwise the fetch time.
            rmt = info.get("regularMarketTime")
            ts = (
                datetime.fromtimestamp(int(rmt), UTC)
                if isinstance(rmt, (int, float))
                else datetime.now(UTC)
            )
            return canonical, Quote(
                symbol=canonical,
                bid=_positive(info.get("bid")),
                ask=_positive(info.get("ask")),
                last=last,
                ts=ts,
                provider=self.name,
            )

        results = await asyncio.gather(*(one(c, ps) for c, ps in items))
        return {c: q for c, q in results if q is not None}

    async def get_bars(
        self,
        symbol: str,
        provider_symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        rows = await self._call(self._yf.history, provider_symbol, INTERVALS[timeframe], start, end)
        bars: list[Bar] = []
        for r in rows:
            o, h, lo, c = (_dec(r.get(k)) for k in ("open", "high", "low", "close"))
            if o is None or h is None or lo is None or c is None:
                continue  # incomplete rows (e.g. NaN) are skipped, never fabricated
            bars.append(
                Bar(
                    ts=_ts(r["ts"]),
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    volume=_dec(r.get("volume")) or Decimal(0),
                )
            )
        bars.sort(key=lambda b: b.ts)
        return bars

    async def get_news(self, symbols: list[tuple[str, str]], limit: int) -> list[NewsItem]:
        items: dict[str, NewsItem] = {}
        for canonical, ps in symbols:
            for raw in await self._call(self._yf.news, ps):
                c = raw.get("content") or {}
                title, pub = c.get("title"), c.get("pubDate")
                if not title or not pub:
                    continue
                nid = str(raw.get("id") or c.get("id") or "")
                if not nid:
                    continue
                if nid in items:
                    prev = items[nid]
                    if canonical not in prev.symbols:
                        items[nid] = NewsItem(
                            **{**prev.__dict__, "symbols": (*prev.symbols, canonical)}
                        )
                    continue
                url = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url")
                items[nid] = NewsItem(
                    id=nid,
                    ts=_ts(pub),
                    headline=title,
                    summary=c.get("summary") or c.get("description"),
                    url=url,
                    source=(c.get("provider") or {}).get("displayName") or "Yahoo Finance",
                    symbols=(canonical,),
                )
        return sorted(items.values(), key=lambda n: n.ts, reverse=True)[:limit]

    async def get_fundamentals(self, symbol: str, provider_symbol: str) -> Fundamentals:
        info = await self._call(self._yf.info, provider_symbol)
        dy = _dec(info.get("dividendYield"))
        extra = {
            k: str(v)
            for k in ("beta", "priceToBook", "forwardPE", "profitMargins", "sharesOutstanding")
            if (v := _dec(info.get(k))) is not None
        }
        return Fundamentals(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            market_cap=_dec(info.get("marketCap")),
            pe=_dec(info.get("trailingPE")),
            eps=_dec(info.get("trailingEps")),
            dividend_yield=dy,  # as reported by yfinance (percent units in recent versions)
            high_52w=_dec(info.get("fiftyTwoWeekHigh")),
            low_52w=_dec(info.get("fiftyTwoWeekLow")),
            currency=info.get("currency"),
            extra=extra,
        )

    async def get_calendar(
        self, symbols: list[tuple[str, str]], start: date, end: date
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []

        def keep(d: date) -> bool:
            return start <= d <= end

        for canonical, ps in symbols:
            cal = await self._call(self._yf.calendar, ps)
            for iso in cal.get("Earnings Date") or []:
                d = date.fromisoformat(str(iso)[:10])
                if keep(d):
                    data = {
                        k: str(v)
                        for k, name in (
                            ("eps_estimate", "Earnings Average"),
                            ("revenue_estimate", "Revenue Average"),
                        )
                        if (v := _dec(cal.get(name))) is not None
                    }
                    events.append(CalendarEvent(canonical, "earnings", d, data))
            for r in await self._call(self._yf.dividends, ps):
                d = date.fromisoformat(r["date"])
                amount = _dec(r.get("amount"))
                if keep(d) and amount is not None:
                    events.append(CalendarEvent(canonical, "dividend", d, {"amount": str(amount)}))
            for r in await self._call(self._yf.splits, ps):
                d = date.fromisoformat(r["date"])
                ratio = _dec(r.get("ratio"))
                if keep(d) and ratio is not None:
                    events.append(CalendarEvent(canonical, "split", d, {"ratio": str(ratio)}))
        return sorted(events, key=lambda e: (e.date, e.symbol, e.kind))

    @staticmethod
    def _hit_class(quote: dict) -> AssetClass | None:
        qt = quote.get("quoteType")
        if qt in ("EQUITY", "ETF"):
            return (
                AssetClass.eu_equity
                if quote.get("exchange") in _EU_EXCHANGES
                else AssetClass.us_equity
            )
        return _QUOTE_TYPE_CLASS.get(qt)

    async def search(self, query: str, limit: int) -> list[InstrumentHit]:
        hits: dict[str, InstrumentHit] = {}
        for q in await self._call(self._yf.search, query):
            sym = q.get("symbol")
            name = q.get("longname") or q.get("shortname")
            if not sym or not name or sym in hits:
                continue
            hits[sym] = InstrumentHit(
                symbol=sym,
                name=name,
                asset_class=self._hit_class(q),
                exchange=q.get("exchDisp") or q.get("exchange"),
                currency=None,
                provider=self.name,
            )
            if len(hits) >= limit:
                break
        return list(hits.values())

    async def screen(self, f: ScreenFilter) -> list[dict]:
        rows = await self._call(self._yf.screen, screen_query(f))
        return [
            {
                "symbol": r.get("symbol"),
                "name": r.get("longName") or r.get("shortName"),
                "price": _dec(r.get("regularMarketPrice")),
                "change_pct": _dec(r.get("regularMarketChangePercent")),
                "market_cap": _dec(r.get("marketCap")),
                "volume": _dec(r.get("regularMarketVolume")),
                "pe": _dec(r.get("trailingPE")),
                "sector": r.get("sector"),
            }
            for r in rows
            if r.get("symbol")
        ]

    async def health_check(self) -> None:
        fast = await self._call(self._yf.fast_info, "SPY")
        if _dec(fast.get("lastPrice")) is None:
            raise RuntimeError("yahoo: no price for SPY")
