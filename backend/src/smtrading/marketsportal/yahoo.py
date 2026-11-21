"""The Yahoo calls of «Mercati v2» (plan 30): batch quotes (v7 quote: prices, previous
close, market state, and the fundamentals and event dates of up to QUOTE_CHUNK symbols
per request), batch sparklines (v7 spark) and one symbol's line chart (v8 chart).

They are for symbols outside the trading catalog too (the real indices of «Mercati
oggi»: ^GSPC, FTSEMIB.MI…), so they do not go through the MarketDataRouter (which
resolves catalog instruments only) but share its allowance: every request takes a token
from the `yahoo` connector's rate limiter (`rl:yahoo`, its per-minute limit) and is
counted in its quota (marketdata.quota), and nothing is asked while the connector is
disabled or demoted after a 429. A refused token is PortalYahooUnavailable, as is any
failure: callers cache what they got and show «non disponibile».

`PortalYahoo(raw=…)`: tests pass a fake raw adapter (`quote`, `spark`, `chart`).
"""

import asyncio
import logging
import time
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.connectors.models import Connector
from smtrading.marketdata import quota
from smtrading.marketdata.ratelimit import acquire

log = logging.getLogger(__name__)

PROVIDER = "yahoo"
QUOTE_CHUNK = 50  # symbols per v7 quote request
SPARK_CHUNK = 20  # symbols per v7 spark request (Yahoo's own limit)
CALL_TIMEOUT_S = 20
CONNECTOR_TTL_S = 60
DEFAULT_RATE = 60
_BASE = "https://query1.finance.yahoo.com"


def _budget(rate: int) -> quota.Budget:
    from smtrading.marketdata.providers.yahoo import YahooProvider

    return quota.budget_of(YahooProvider, rate, None)


class PortalYahooUnavailable(Exception):
    """Yahoo did not answer, or its allowance is used up / the connector is off."""


class RawYahoo(Protocol):
    def quote(self, symbols: list[str], fields: list[str]) -> list[dict]: ...
    def spark(self, symbols: list[str], range_: str, interval: str) -> dict[str, dict]: ...
    def chart(self, symbol: str, range_: str, interval: str) -> dict: ...


class _YfRaw:
    """Synchronous requests through yfinance's crumb-carrying session (the provider's)."""

    def _get(self, url: str, params: dict) -> Any:
        from yfinance.data import YfData

        from smtrading.marketdata.providers.yahoo import _session

        return YfData(session=_session()).get_raw_json(url, params=params)

    def quote(self, symbols: list[str], fields: list[str]) -> list[dict]:
        data = self._get(f"{_BASE}/v7/finance/quote",
                         {"symbols": ",".join(symbols), "fields": ",".join(fields)})
        return list(((data or {}).get("quoteResponse") or {}).get("result") or [])

    def spark(self, symbols: list[str], range_: str, interval: str) -> dict[str, dict]:
        data = self._get(f"{_BASE}/v7/finance/spark",
                         {"symbols": ",".join(symbols), "range": range_, "interval": interval})
        out: dict[str, dict] = {}
        for r in ((data or {}).get("spark") or {}).get("result") or []:
            resp = (r.get("response") or [None])[0] or {}
            out[str(r.get("symbol"))] = resp
        return out

    def chart(self, symbol: str, range_: str, interval: str) -> dict:
        from urllib.parse import quote as urlquote

        data = self._get(f"{_BASE}/v8/finance/chart/{urlquote(symbol, safe='')}",
                         {"range": range_, "interval": interval})
        return (((data or {}).get("chart") or {}).get("result") or [None])[0] or {}


def series(resp: dict) -> list[tuple[int, float]]:
    """(epoch seconds, close) of a spark/chart response, gaps (null closes) left out."""
    ts = resp.get("timestamp") or []
    closes = (((resp.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    out = []
    for t, c in zip(ts, closes, strict=False):
        if isinstance(t, (int, float)) and isinstance(c, (int, float)) and not isinstance(c, bool):
            out.append((int(t), float(c)))
    return out


class PortalYahoo:
    def __init__(self, redis, sessionmaker: async_sessionmaker | None, *,
                 raw: RawYahoo | None = None) -> None:
        self._redis = redis
        self._sm = sessionmaker
        self._raw: RawYahoo = raw or _YfRaw()
        self._conn: tuple[float, bool, int] | None = None

    async def _connector(self) -> tuple[bool, int]:
        """(enabled, per-minute limit) of the `yahoo` connector, read at most every minute
        (no sessionmaker, as in unit tests: enabled at the default limit)."""
        now = time.monotonic()
        if self._conn is not None and now - self._conn[0] < CONNECTOR_TTL_S:
            return self._conn[1], self._conn[2]
        enabled, rate = True, DEFAULT_RATE
        if self._sm is not None:
            async with self._sm() as session:
                c = await session.get(Connector, PROVIDER)
            enabled = bool(c and c.enabled)
            if c is not None and c.rate_limit_per_min is not None:
                rate = c.rate_limit_per_min
        self._conn = (now, enabled, rate)
        return enabled, rate

    async def _gate(self) -> None:
        enabled, rate = await self._connector()
        if not enabled:
            raise PortalYahooUnavailable("yahoo connector disabled")
        try:
            used = (await quota.usage(self._redis, [PROVIDER])).get(PROVIDER)
            why = used.demoted(_budget(rate)) if used is not None else None
            if why is not None:
                # the trading side comes first: the portal stops near the budget
                raise PortalYahooUnavailable(f"yahoo quota near its limit ({why})")
            ok = await acquire(self._redis, PROVIDER, rate)
        except PortalYahooUnavailable:
            raise
        except Exception:
            log.warning("yahoo rate limiter unavailable; proceeding", exc_info=True)
            return
        if not ok:
            raise PortalYahooUnavailable("yahoo rate limited")
        await quota.count(self._redis, PROVIDER, 1)

    async def _call(self, fn, *args) -> Any:
        await self._gate()
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), CALL_TIMEOUT_S)
        except Exception as e:
            text = str(e)
            if quota.is_quota_error(e, text):
                try:
                    await quota.block(self._redis, PROVIDER, _budget(DEFAULT_RATE))
                except Exception:
                    log.warning("yahoo quota block failed", exc_info=True)
            raise PortalYahooUnavailable(text[:200]) from e

    async def quotes(self, symbols: list[str], fields: list[str]) -> dict[str, dict]:
        """{symbol: v7 quote row} for the symbols Yahoo knows, QUOTE_CHUNK per request. A
        failing chunk is left out (logged); raises only when nothing came back and a
        request failed."""
        out: dict[str, dict] = {}
        error: PortalYahooUnavailable | None = None
        for i in range(0, len(symbols), QUOTE_CHUNK):
            chunk = symbols[i:i + QUOTE_CHUNK]
            try:
                rows = await self._call(self._raw.quote, chunk, fields)
            except PortalYahooUnavailable as e:
                log.info("yahoo quote chunk failed: %s", e)
                error = error or e
                continue
            for r in rows:
                if r.get("symbol"):
                    out[str(r["symbol"])] = r
        if not out and error is not None:
            raise error
        return out

    async def spark(self, symbols: list[str], range_: str, interval: str
                    ) -> dict[str, list[tuple[int, float]]]:
        out: dict[str, list[tuple[int, float]]] = {}
        for i in range(0, len(symbols), SPARK_CHUNK):
            chunk = symbols[i:i + SPARK_CHUNK]
            got = await self._call(self._raw.spark, chunk, range_, interval)
            for sym, resp in got.items():
                out[sym] = series(resp)
        return out

    async def chart(self, symbol: str, range_: str, interval: str) -> list[tuple[int, float]]:
        return series(await self._call(self._raw.chart, symbol, range_, interval))
