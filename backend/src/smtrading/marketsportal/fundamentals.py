"""Fundamentals of the instrument sheet and the event dates of the week's calendar
(plan 30), from Yahoo's batch v7 quote: market cap, P/E (trailing and forward), dividend
yield, 52-week range, beta, next earnings date, ex-dividend date — QUOTE_CHUNK symbols
per request, each symbol cached TTL_S (12 h; a symbol Yahoo does not know is cached as
empty too). When Yahoo fails for one sheet, the market data router's fundamentals
(Finnhub / Yahoo info, its own cache) fill what they can; whatever is missing is null
(«non disponibile» on the page).
"""

import asyncio
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError

from smtrading.common.errors import SMTError
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketsportal.yahoo import PortalYahoo, PortalYahooUnavailable

log = logging.getLogger(__name__)

TTL_S = 12 * 3600
KEY = "portal:fund:v1:"
FIELDS = ["symbol", "currency", "marketCap", "trailingPE", "forwardPE", "dividendYield",
          "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "beta", "earningsTimestamp",
          "earningsTimestampStart", "earningsTimestampEnd", "isEarningsDateEstimate",
          "exDividendDate", "dividendDate"]
EMPTY_KEYS = ("market_cap", "pe_trailing", "pe_forward", "dividend_yield", "low_52w",
              "high_52w", "beta", "next_earnings", "earnings_estimate", "ex_dividend",
              "dividend_date", "currency")


def yahoo_symbol(i: Instrument) -> str:
    """The instrument's Yahoo symbol (its override, else the provider's own mapping)."""
    override = (i.provider_symbols or {}).get("yahoo")
    if override:
        return override
    if i.asset_class == AssetClass.forex and len(i.symbol) == 6 and i.symbol.isalpha():
        return f"{i.symbol}=X"
    return i.symbol


def _num(v: Any) -> str | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    d = Decimal(str(v))
    return str(d) if d.is_finite() else None


def _day(v: Any) -> date | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return datetime.fromtimestamp(int(v), UTC).date()


def parse(row: dict, today: date) -> dict:
    """The sheet's figures from a v7 quote row. `dividend_yield` in percent units (Yahoo's
    v7 `dividendYield`: 0.31 = 0,31%). `next_earnings`: the first of Yahoo's earnings
    dates on or after `today` (`earnings_estimate`: the company has not confirmed it)."""
    upcoming = sorted(d for k in ("earningsTimestamp", "earningsTimestampStart",
                                  "earningsTimestampEnd")
                      if (d := _day(row.get(k))) is not None and d >= today)
    ex = _day(row.get("exDividendDate"))
    pay = _day(row.get("dividendDate"))
    return {
        "market_cap": _num(row.get("marketCap")),
        "pe_trailing": _num(row.get("trailingPE")),
        "pe_forward": _num(row.get("forwardPE")),
        "dividend_yield": _num(row.get("dividendYield")),
        "low_52w": _num(row.get("fiftyTwoWeekLow")),
        "high_52w": _num(row.get("fiftyTwoWeekHigh")),
        "beta": _num(row.get("beta")),
        "next_earnings": upcoming[0].isoformat() if upcoming else None,
        "earnings_estimate": bool(row.get("isEarningsDateEstimate")) if upcoming else None,
        "ex_dividend": ex.isoformat() if ex else None,
        "dividend_date": pay.isoformat() if pay else None,
        "currency": row.get("currency") if isinstance(row.get("currency"), str) else None,
    }


def _from_router(f) -> dict:
    """The router's Fundamentals (Finnhub / Yahoo info) in the sheet's shape."""
    extra = f.extra or {}
    return {
        **dict.fromkeys(EMPTY_KEYS),
        "market_cap": str(f.market_cap) if f.market_cap is not None else None,
        "pe_trailing": str(f.pe) if f.pe is not None else None,
        "pe_forward": extra.get("forwardPE"),
        "dividend_yield": str(f.dividend_yield) if f.dividend_yield is not None else None,
        "low_52w": str(f.low_52w) if f.low_52w is not None else None,
        "high_52w": str(f.high_52w) if f.high_52w is not None else None,
        "beta": extra.get("beta"),
        "currency": f.currency,
    }


class FundamentalsService:
    def __init__(self, redis, yahoo: PortalYahoo, *, clock=lambda: datetime.now(UTC)) -> None:
        self._redis = redis
        self._yahoo = yahoo
        self._clock = clock
        self._lock = asyncio.Lock()  # one catalog-wide refresh at a time

    async def _read(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        try:
            raws = await self._redis.mget([KEY + s for s in symbols])
        except RedisError:
            log.warning("fundamentals cache read failed", exc_info=True)
            return {}
        return {s: json.loads(r) for s, r in zip(symbols, raws, strict=True) if r is not None}

    async def _write(self, sym: str, body: dict) -> None:
        try:
            await self._redis.set(KEY + sym, json.dumps(body), ex=TTL_S)
        except RedisError:
            log.warning("fundamentals cache write failed", exc_info=True)

    async def many(self, instruments: list[Instrument]) -> dict[str, dict]:
        """symbol -> figures (cached; the missing ones in batch requests). A symbol whose
        request failed is absent (asked again next time)."""
        by_sym = {i.symbol: i for i in instruments}
        out = await self._read(list(by_sym))
        todo = [i for s, i in by_sym.items() if s not in out]
        if not todo:
            return out
        async with self._lock:
            out.update(await self._read([i.symbol for i in todo]))  # another request did it
            todo = [i for i in todo if i.symbol not in out]
            if not todo:
                return out
            ys = {yahoo_symbol(i): i.symbol for i in todo}
            try:
                rows = await self._yahoo.quotes(list(ys), FIELDS)
            except PortalYahooUnavailable as e:
                log.info("fundamentals unavailable: %s", e)
                return out
            today = self._clock().astimezone(UTC).date()
            for ysym, sym in ys.items():
                row = rows.get(ysym)
                body = {**parse(row, today), "source": "yahoo"} if row is not None \
                    else {**dict.fromkeys(EMPTY_KEYS), "source": None}
                out[sym] = body
                await self._write(sym, body)
        return out

    async def one(self, instrument: Instrument, router=None) -> dict:
        got = (await self.many([instrument])).get(instrument.symbol)
        if got is not None and got.get("source"):
            return got
        if router is not None:
            try:
                f = await router.get_fundamentals(instrument.symbol)
                body = {**_from_router(f), "source": "router"}
                await self._write(instrument.symbol, body)
                return body
            except (SMTError, AttributeError):
                pass
        return got or {**dict.fromkeys(EMPTY_KEYS), "source": None}
