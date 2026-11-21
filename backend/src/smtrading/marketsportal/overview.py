"""«Mercati oggi» (plan 30): the real index values (not ETFs), Bitcoin, EUR/USD, gold,
oil and the US 10-year yield, from Yahoo's index symbols.

One snapshot = one batch v7 quote request (value, previous close, time) plus one or two
batch spark requests (intraday 5-minute closes for the items whose market is open, five
days of hourly closes for the others), cached OPEN_TTL_S while a market of the bar is
open and CLOSED_TTL_S when every one is closed (crypto, always open, does not count).
A failed refresh answers the last good snapshot marked `stale` (kept LAST_TTL_S).

These symbols are portal only: they are never in the trading catalog, never tracked by
the stream, never visible over MCP.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError

from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import market_status
from smtrading.marketsportal.yahoo import PortalYahoo, PortalYahooUnavailable

log = logging.getLogger(__name__)

OPEN_TTL_S = 60
CLOSED_TTL_S = 300
LAST_TTL_S = 7 * 24 * 3600
SNAPSHOT_KEY = "portal:overview:v1"
LAST_KEY = "portal:overview:last:v1"
CHART_KEY = "portal:overview:chart:v1:"
SPARK_POINTS = 60
QUOTE_FIELDS = ["symbol", "shortName", "regularMarketPrice", "regularMarketPreviousClose",
                "regularMarketTime", "marketState", "currency"]


@dataclass(frozen=True)
class Item:
    code: str  # our id (URL-safe), also what a personal alert names
    symbol: str  # Yahoo's
    label: str
    group: str
    market: str  # market_hours market whose hours it follows
    unit: str = ""  # "%" for the yield
    decimals: int = 2
    note: str = ""


ITEMS: tuple[Item, ...] = (
    Item("SPX", "^GSPC", "S&P 500", "Stati Uniti", "NYSE"),
    Item("NDX", "^NDX", "Nasdaq 100", "Stati Uniti", "NASDAQ"),
    Item("DJI", "^DJI", "Dow Jones", "Stati Uniti", "NYSE"),
    Item("RUT", "^RUT", "Russell 2000", "Stati Uniti", "NYSE"),
    Item("FTSEMIB", "FTSEMIB.MI", "FTSE MIB", "Europa", "MTA"),
    Item("DAX", "^GDAXI", "DAX", "Europa", "XETRA"),
    Item("CAC40", "^FCHI", "CAC 40", "Europa", "EURONEXT_PARIS"),
    Item("SX5E", "^STOXX50E", "Euro Stoxx 50", "Europa", "XETRA"),
    Item("VIX", "^VIX", "VIX", "Volatilità e tassi", "NYSE"),
    Item("TNX", "^TNX", "Treasury 10 anni", "Volatilità e tassi", "NYSE", unit="%",
         decimals=3, note="rendimento in %"),
    Item("BTC", "BTC-USD", "Bitcoin", "Cripto e valute", "CRYPTO", decimals=0),
    Item("EURUSD", "EURUSD=X", "EUR/USD", "Cripto e valute", "FX", decimals=4),
    Item("GOLD", "GC=F", "Oro", "Materie prime", "FX",
         note="future COMEX (scadenza più vicina); orari approssimati"),
    Item("WTI", "CL=F", "Petrolio WTI", "Materie prime", "FX",
         note="future NYMEX (scadenza più vicina); orari approssimati"),
    Item("BRENT", "BZ=F", "Petrolio Brent", "Materie prime", "FX",
         note="future ICE (scadenza più vicina); orari approssimati"),
)
BY_CODE = {i.code: i for i in ITEMS}

# the overview chart: range -> (Yahoo range, interval, cache seconds)
CHART_RANGES: dict[str, tuple[str, str, int]] = {
    "1d": ("1d", "5m", 60),
    "5d": ("5d", "30m", 300),
    "1mo": ("1mo", "1d", 3600),
    "1y": ("1y", "1d", 6 * 3600),
}


def _num(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def _thin(points: list[tuple[int, float]], n: int = SPARK_POINTS) -> list[list[float]]:
    """At most n points, evenly picked, the last always kept."""
    if len(points) <= n:
        return [[t, v] for t, v in points]
    step = (len(points) - 1) / (n - 1)
    picked = [points[round(k * step)] for k in range(n - 1)] + [points[-1]]
    return [[t, v] for t, v in picked]


def _status(market: str, now: datetime) -> dict:
    try:
        s = market_status(market, now)
    except (SMTError, KeyError, ValueError):
        return {"is_open": None, "next_open": None, "next_close": None}
    return {"is_open": s.is_open,
            "next_open": s.next_open.isoformat() if s.next_open else None,
            "next_close": s.next_close.isoformat() if s.next_close else None}


def any_open(statuses: dict[str, dict]) -> bool:
    """A market of the bar other than crypto (always open) is open."""
    return any(s.get("is_open") for m, s in statuses.items() if m != "CRYPTO")


class OverviewService:
    def __init__(self, redis, yahoo: PortalYahoo, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._redis = redis
        self._yahoo = yahoo
        self._clock = clock

    async def _get(self, key: str) -> dict | None:
        try:
            raw = await self._redis.get(key)
        except RedisError:
            log.warning("overview cache read failed", exc_info=True)
            return None
        return json.loads(raw) if raw is not None else None

    async def _set(self, key: str, body: dict, ttl: int) -> None:
        try:
            await self._redis.set(key, json.dumps(body), ex=ttl)
        except RedisError:
            log.warning("overview cache write failed", exc_info=True)

    async def snapshot(self) -> dict:
        if (hit := await self._get(SNAPSHOT_KEY)) is not None:
            return hit
        now = self._clock().astimezone(UTC)
        statuses = {m: _status(m, now) for m in {i.market for i in ITEMS}}
        ttl = OPEN_TTL_S if any_open(statuses) else CLOSED_TTL_S
        try:
            rows = await self._yahoo.quotes([i.symbol for i in ITEMS], QUOTE_FIELDS)
        except PortalYahooUnavailable as e:
            log.info("overview: quotes unavailable: %s", e)
            last = await self._get(LAST_KEY)
            if last is not None:
                body = {**last, "stale": True}
            else:
                body = self._body(now, statuses, {}, {}, ttl)
                body["error"] = "non disponibile"
            await self._set(SNAPSHOT_KEY, body, OPEN_TTL_S)  # retried within the minute
            return body
        sparks: dict[str, list[tuple[int, float]]] = {}
        open_syms = [i.symbol for i in ITEMS if statuses[i.market].get("is_open")]
        closed_syms = [i.symbol for i in ITEMS if i.symbol not in open_syms]
        for syms, rng, interval in ((open_syms, "1d", "5m"), (closed_syms, "5d", "1h")):
            if not syms:
                continue
            try:
                sparks.update(await self._yahoo.spark(syms, rng, interval))
            except PortalYahooUnavailable as e:
                log.info("overview: sparklines unavailable: %s", e)
        body = self._body(now, statuses, rows, sparks, ttl)
        await self._set(SNAPSHOT_KEY, body, ttl)
        if any(it["value"] is not None for it in body["items"]):
            await self._set(LAST_KEY, body, LAST_TTL_S)
        return body

    def _body(self, now: datetime, statuses: dict, rows: dict, sparks: dict, ttl: int) -> dict:
        items = []
        for i in ITEMS:
            r = rows.get(i.symbol) or {}
            value, prev = _num(r.get("regularMarketPrice")), _num(
                r.get("regularMarketPreviousClose"))
            change = ((value / prev - 1) * 100).quantize(Decimal("0.01")) \
                if value is not None and prev else None
            t = r.get("regularMarketTime")
            ts = datetime.fromtimestamp(int(t), UTC).isoformat() \
                if isinstance(t, (int, float)) and not isinstance(t, bool) else None
            items.append({
                "code": i.code, "symbol": i.symbol, "label": i.label, "group": i.group,
                "unit": i.unit, "decimals": i.decimals, "note": i.note,
                "currency": r.get("currency"), "market": i.market,
                "value": str(value) if value is not None else None,
                "prev_close": str(prev) if prev is not None else None,
                "change_pct": str(change) if change is not None else None,
                "ts": ts, **statuses[i.market],
                "spark": _thin(sparks.get(i.symbol) or []),
            })
        return {"as_of": now.isoformat(), "ttl_s": ttl, "stale": False, "items": items}

    async def prices(self) -> dict[str, Decimal]:
        """code -> latest value (the cached snapshot; refreshed when expired)."""
        snap = await self.snapshot()
        return {it["code"]: Decimal(it["value"]) for it in snap.get("items") or []
                if it.get("value") is not None}

    async def chart(self, code: str, range_: str) -> dict:
        item = BY_CODE[code]
        rng, interval, ttl = CHART_RANGES[range_]
        key = f"{CHART_KEY}{code}:{range_}"
        if (hit := await self._get(key)) is not None:
            return hit
        try:
            points = await self._yahoo.chart(item.symbol, rng, interval)
            body = {"code": code, "range": range_, "label": item.label,
                    "points": [[t, v] for t, v in points], "error": None}
        except PortalYahooUnavailable as e:
            log.info("overview chart %s %s unavailable: %s", code, range_, e)
            body = {"code": code, "range": range_, "label": item.label, "points": [],
                    "error": "non disponibile"}
            ttl = OPEN_TTL_S
        await self._set(key, body, ttl)
        return body
