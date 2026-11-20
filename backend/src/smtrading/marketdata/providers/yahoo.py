import asyncio
import logging
import math
import struct
import threading
import time
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
    DayChange,
    ExtendedQuote,
    Fundamentals,
    InstrumentHit,
    NewsItem,
    Quote,
    ScreenFilter,
    Timeframe,
)

log = logging.getLogger(__name__)
MAX_CONCURRENT_CALLS = 4
_NO_INFO_QUOTE_TYPES = frozenset({"CURRENCY", "CRYPTOCURRENCY"})
_RETRY_DELAY_S = 0.5
_session_lock = threading.Lock()
_shared_session: Any = None


def _session() -> Any:
    """The one curl_cffi session every yfinance call goes through, forced to HTTP/1.1:
    Yahoo's HTTP/2 endpoint intermittently fails with curl (16) framing errors. yfinance keeps a
    process-wide YfData singleton that adopts the session of each call, so per-thread sessions
    would keep swapping its cookie jar: one shared session (curl_cffi keeps a curl handle per
    thread) is the safe choice."""
    global _shared_session
    with _session_lock:
        if _shared_session is None:
            from curl_cffi import CurlHttpVersion
            from curl_cffi import requests as curl_requests

            # No yf.set_tz_cache_location: yfinance's sqlite caches raise "database is
            # locked" under our concurrent threads; the unwritable default leaves them off.
            _shared_session = curl_requests.Session(
                impersonate="chrome", http_version=CurlHttpVersion.V1_1
            )
        return _shared_session


def _transient(exc: BaseException) -> bool:
    """A curl transport failure (HTTP/2 framing, reset, timeout...), not an HTTP status."""
    from curl_cffi import CurlError
    from curl_cffi.const import CurlECode

    code = getattr(exc, "code", 0)
    return isinstance(exc, CurlError) and code not in (0, CurlECode.HTTP_RETURNED_ERROR)


def _attempt(fn: Any, retry_if: Any = None) -> Any:
    """fn(), retried once after a transient curl error or when retry_if(result) holds (yfinance
    swallows transport errors in history/fast_info and reports an empty, "possibly delisted"
    result). A symbol that is really unknown comes back the same way on the retry."""
    try:
        out = fn()
    except Exception as exc:
        if not _transient(exc):
            raise
        log.info("yahoo: transient error, retrying once: %s", exc)
    else:
        if retry_if is None or not retry_if(out):
            return out
    time.sleep(_RETRY_DELAY_S)
    return fn()


def _ticker(sym: str) -> Any:
    import yfinance as yf

    return yf.Ticker(sym, session=_session())


INTERVALS = {
    Timeframe.m1: "1m",
    Timeframe.m5: "5m",
    Timeframe.m15: "15m",
    Timeframe.h1: "60m",
    Timeframe.d1: "1d",
    Timeframe.w1: "1wk",
}

_QUOTE_TYPE_CLASS = {
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
_EU_REGIONS = ("it", "de", "fr", "nl", "es", "gb", "ch")


def _as_float32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def _float32_text(x: float) -> str:
    """yfinance widens float32 prices (358.53 -> 358.5299987792969): a value exactly
    representable as float32 is written with the shortest digits that round-trip to it.
    Any other double is kept as is."""
    try:
        if _as_float32(x) != x:
            return str(x)
    except OverflowError:
        return str(x)
    for digits in range(1, 10):
        text = f"{x:.{digits}g}"
        if _as_float32(float(text)) == x:
            return repr(float(text))
    return str(x)


def _dec(value: object) -> Decimal | None:
    """Decimal(str(x)); None and NaN/inf become None. Booleans are not numbers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, float):
        value = _float32_text(value)
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


_SESSION_INTERVALS = frozenset({"1d", "5d", "1wk", "1mo", "3mo"})


def _bar_ts(local: datetime, interval: str) -> datetime:
    """Intraday candles: the exact instant in UTC. Daily and longer: Yahoo stamps them at
    the exchange's local midnight (a Milan Friday = Thursday 23:00 UTC), so they carry the
    local session date at 00:00 UTC instead."""
    if interval in _SESSION_INTERVALS and local.tzinfo is not None:
        return datetime(local.year, local.month, local.day, tzinfo=UTC)
    return local.astimezone(UTC) if local.tzinfo is not None else local.replace(tzinfo=UTC)


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
        def once() -> dict:
            fi = _ticker(sym).fast_info
            return self._clean({k: fi[k] for k in fi})

        return _attempt(once, lambda d: d.get("lastPrice") is None)

    def history(self, sym: str, interval: str, start: datetime, end: datetime) -> list[dict]:
        def once() -> list[dict]:
            df = _ticker(sym).history(interval=interval, start=start, end=end, auto_adjust=False)
            return [
                self._clean(
                    {
                        "ts": _bar_ts(ts.to_pydatetime(), interval),
                        "open": r["Open"],
                        "high": r["High"],
                        "low": r["Low"],
                        "close": r["Close"],
                        "volume": r["Volume"],
                    }
                )
                for ts, r in df.iterrows()
            ]

        return _attempt(once, lambda rows: not rows)

    def info(self, sym: str) -> dict:
        return _attempt(lambda: self._clean(_ticker(sym).info))

    def extended_hours(self, sym: str) -> dict:
        """The pre-market / after-hours fields of info: prices and epoch times (None when
        missing), and the latest regular-session price and time."""
        info = self.info(sym)
        return {
            name: info.get(key)
            for name, key in (
                ("pre_price", "preMarketPrice"), ("pre_time", "preMarketTime"),
                ("post_price", "postMarketPrice"), ("post_time", "postMarketTime"),
                ("regular_price", "regularMarketPrice"), ("regular_time", "regularMarketTime"),
            )
        }

    def batch_quote(self, syms: list[str]) -> list[dict]:
        """One v7 quote request for many symbols (yfinance adds the crumb): symbol, regular
        price, previous close and time of each symbol Yahoo knows."""
        from yfinance.data import YfData

        fields = "symbol,regularMarketPrice,regularMarketPreviousClose,regularMarketTime"

        def once() -> list[dict]:
            data = YfData(session=_session()).get_raw_json(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                params={"symbols": ",".join(syms), "fields": fields},
            )
            rows = ((data or {}).get("quoteResponse") or {}).get("result") or []
            return [self._clean({k: r.get(k) for k in fields.split(",")}) for r in rows]

        return _attempt(once)

    def news(self, sym: str) -> list[dict]:
        return _attempt(lambda: self._clean(_ticker(sym).news))

    def calendar(self, sym: str) -> dict:
        return _attempt(lambda: self._clean(_ticker(sym).calendar))

    def dividends(self, sym: str) -> list[dict]:
        def once() -> list[dict]:
            s = _ticker(sym).dividends
            return [{"date": d.date().isoformat(), "amount": self._clean(v)} for d, v in s.items()]

        return _attempt(once)

    def splits(self, sym: str) -> list[dict]:
        def once() -> list[dict]:
            s = _ticker(sym).splits
            return [{"date": d.date().isoformat(), "ratio": self._clean(v)} for d, v in s.items()]

        return _attempt(once)

    def search(self, q: str) -> list[dict]:
        import yfinance as yf

        return _attempt(
            lambda: self._clean(
                yf.Search(q, max_results=20, news_count=0, session=_session()).quotes
            )
        )

    def screen(self, query: dict) -> list[dict]:
        import yfinance as yf

        def build(node: dict) -> Any:
            ops = [build(o) if isinstance(o, dict) else o for o in node["operands"]]
            # leaf values arrive as strings; numeric comparisons need numbers
            if node["operator"] not in ("AND", "OR", "EQ"):
                ops = [ops[0], float(ops[1])]
            return yf.EquityQuery(node["operator"].lower(), ops)

        q = build(query["query"])
        res = _attempt(lambda: yf.screen(q, size=query["limit"], session=_session()))
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
            Capability.fx,
            Capability.search,
            Capability.screener,
        }
    )
    asset_classes = frozenset(AssetClass)
    requires_key = False
    default_rate_limit_per_min = 60
    default_hourly_budget = 2000  # unofficial API: a soft limit of our own

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
        sem = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

        async def one(canonical: str, ps: str) -> tuple[str, Quote | None]:
            async with sem:
                fast = await self._call(self._yf.fast_info, ps)
            last = _dec(fast.get("lastPrice"))
            if last is None:
                return canonical, None
            info: dict = {}
            # forex and crypto have no meaningful bid/ask/time in info: skip the extra call.
            if fast.get("quoteType") not in _NO_INFO_QUOTE_TYPES:
                try:
                    async with sem:
                        info = await self._call(self._yf.info, ps) or {}
                except Exception as exc:  # noqa: BLE001 - info is best-effort enrichment
                    log.warning("yahoo info failed for %s: %s", ps, exc)
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

        results = await asyncio.gather(*(one(c, ps) for c, ps in items), return_exceptions=True)
        out: dict[str, Quote] = {}
        first_error: BaseException | None = None
        for (canonical, _), res in zip(items, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("yahoo quote failed for %s: %s", canonical, res)
                first_error = first_error or res
            elif res[1] is not None:
                out[canonical] = res[1]
        # nothing at all and at least one call raised: an outage, not missing symbols
        if not out and first_error is not None:
            raise first_error
        return out

    day_change_chunk = 50  # symbols per v7 quote request
    day_change_cost = 1

    async def get_day_changes(self, items: list[tuple[str, str]]) -> dict[str, DayChange]:
        """One v7 quote request: the regular market price against the previous close."""
        by_ps: dict[str, list[str]] = {}
        for canonical, ps in items:
            by_ps.setdefault(ps, []).append(canonical)
        rows = await self._call(self._yf.batch_quote, list(by_ps))
        out: dict[str, DayChange] = {}
        for r in rows:
            last, prev = _positive(r.get("regularMarketPrice")), _positive(
                r.get("regularMarketPreviousClose"))
            t = r.get("regularMarketTime")
            if last is None or prev is None or isinstance(t, bool) \
                    or not isinstance(t, (int, float)):
                continue
            for canonical in by_ps.get(str(r.get("symbol")), []):
                out[canonical] = DayChange(
                    canonical, last, prev, datetime.fromtimestamp(int(t), UTC), self.name)
        return out

    async def get_extended(self, symbol: str, provider_symbol: str) -> ExtendedQuote | None:
        """The latest pre-market or after-hours price traded after the latest regular
        session (the newer of the two), or None. Information only."""
        d = await self._call(self._yf.extended_hours, provider_symbol)
        close = _positive(d.get("regular_price"))
        rt = d.get("regular_time")
        best: tuple[float, str, Decimal] | None = None
        for session in ("pre", "post"):
            price, t = _positive(d.get(f"{session}_price")), d.get(f"{session}_time")
            if price is None or isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            if isinstance(rt, (int, float)) and t <= rt:
                continue  # earlier than the latest regular session: superseded
            if best is None or t > best[0]:
                best = (t, session, price)
        if best is None or close is None:
            return None
        t, session, price = best
        return ExtendedQuote(
            symbol=symbol, session=session,  # type: ignore[arg-type]
            price=price, regular_close=close,
            ts=datetime.fromtimestamp(int(t), UTC), provider=self.name,
        )

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        fast = await self._call(self._yf.fast_info, f"{base}{quote}=X")
        rate = _positive(fast.get("lastPrice"))
        if rate is None:
            raise RuntimeError(f"yahoo: no rate for {base}{quote}")
        return rate

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
            dividend_yield=dy,  # percent units (0.3 = 0.3%), as yfinance reports it
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
            currency: str | None = None
            currency_known = False
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
                    if not currency_known:  # only looked up when a dividend is reported
                        currency, currency_known = await self._currency(ps), True
                    events.append(
                        CalendarEvent(
                            canonical, "dividend", d, {"amount": str(amount), "currency": currency}
                        )
                    )
            for r in await self._call(self._yf.splits, ps):
                d = date.fromisoformat(r["date"])
                ratio = _dec(r.get("ratio"))
                if keep(d) and ratio is not None:
                    events.append(CalendarEvent(canonical, "split", d, {"ratio": str(ratio)}))
        return sorted(events, key=lambda e: (e.date, e.symbol, e.kind))

    async def _currency(self, ps: str) -> str | None:
        """Trading currency from fast_info (falls back to None: callers use the
        instrument's currency)."""
        try:
            fast = await self._call(self._yf.fast_info, ps)
        except Exception as exc:  # noqa: BLE001 - enrichment only
            log.warning("yahoo currency lookup failed for %s: %s", ps, exc)
            return None
        cur = fast.get("currency")
        return cur if isinstance(cur, str) and cur else None

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
