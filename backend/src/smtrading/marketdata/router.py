"""MarketDataRouter: the single entry point for market data.

Resolves the asset class of each symbol, reads the cache, then walks the providers in
priority order (skipping unregistered, disabled and rate-limited ones) with a timeout,
falling back on errors. Never lets a raw exception escape: callers always get SMTError.
"""

import asyncio
import functools
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Importing the providers package registers every built-in provider in PROVIDERS, so router
# users need no separate import. Providers depend only on provider/registry/types (never on
# the router), so this cannot cycle.
import smtrading.marketdata.providers  # noqa: F401
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector
from smtrading.connectors.service import (
    get_connector_config,
    get_priorities,
    list_connectors,
    record_health,
)
from smtrading.instruments.models import AssetClass
from smtrading.instruments.service import canonical_symbol, get_instrument
from smtrading.marketdata import serialization
from smtrading.marketdata.bar_close import bar_is_closed, bars_cache_ttl
from smtrading.marketdata.cache import (
    CACHE_TTL,
    cache_get,
    cache_set,
    read_quote_meta,
    write_quote,
)
from smtrading.marketdata.provider import DataProvider, ProviderUnsupported
from smtrading.marketdata.ratelimit import acquire
from smtrading.marketdata.registry import PROVIDERS
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

log = logging.getLogger(__name__)

PROVIDER_TIMEOUT_S = 10
QUOTE_TIMEOUT_PER_ITEM_S = 2  # batch quote calls: timeout grows with the batch size
BARS_FLOOR_MAX = timedelta(hours=1)
# How late a provider's first bar may start, past the requested start, and still count as
# covering the range: 2 bar intervals, plus weekends/holidays for markets that close.
BARS_COVERAGE_INTERVALS = 2
BARS_CLOSED_MARKET_SLACK = timedelta(days=4)
INSTANCE_MAX_AGE_S = 60
RATE_LIMITED = "rate_limited"

_MISS = object()


@dataclass(frozen=True)
class _Instr:
    symbol: str
    asset_class: AssetClass
    provider_symbols: dict
    market: str | None = None  # the session rule of daily/weekly bars (bar_close)

    def override(self, provider: str) -> str | None:
        return self.provider_symbols.get(provider)


@dataclass(frozen=True)
class _Slot:
    name: str
    provider: DataProvider | None  # None: lacks the capability, or failed to build
    rate_limit: int
    build_error: str | None = None  # "Type: msg" when construction/config failed


@dataclass
class _Outcome:
    """Accumulates why providers failed, to pick the final error code."""

    errors: dict[str, str] = field(default_factory=dict)
    unsupported: bool = False
    attempted: bool = False  # at least one enabled, registered provider was considered

    def merge(self, other: "_Outcome") -> None:
        self.errors.update(other.errors)
        self.unsupported |= other.unsupported
        self.attempted |= other.attempted

    def error(self, capability: Capability, details: dict) -> SMTError:
        if not self.attempted:
            return SMTError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"no enabled provider for {capability}",
                {"errors": {}, "reason": "no_enabled_provider", **details},
            )
        if not self.errors and self.unsupported:
            return SMTError(
                ErrorCode.SYMBOL_NOT_FOUND, f"no provider supports this {capability}", details
            )
        return SMTError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"no provider available for {capability}",
            {"errors": dict(self.errors), **details},
        )


_URL_QUERY = re.compile(r"(https?://[^\s?]+)\?\S*")
_SECRET_PARAM = re.compile(r"(?i)\b(token|apikey|api_key|key|secret)=[^&\s'\"]+")


def _redact(text: str) -> str:
    """Strip query strings from URLs and secret-looking query params: errors reach MCP agents and logs."""
    return _SECRET_PARAM.sub(r"\1=<redacted>", _URL_QUERY.sub(r"\1?<redacted>", text))


def _describe(e: BaseException) -> str:
    return _redact(f"{type(e).__name__}: {e}")


def _guard(fn):
    """Translate any unexpected exception (DB, Redis, bugs) into an SMTError."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except SMTError:
            raise
        except Exception as e:
            log.exception("market data router failure in %s", fn.__name__)
            raise SMTError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "market data unavailable",
                {"error": f"{type(e).__name__}: {e}"},
            ) from e

    return wrapper


def _invalid(message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, details or None)


def _cache_key(capability: Capability, args: dict) -> str:
    digest = hashlib.sha1(serialization.dumps(args).encode()).hexdigest()
    return f"md:{capability}:{digest}"


def _dumps_list(items: list) -> str:
    return serialization.dumps(items)


def _loads_list[T](cls: type[T]) -> Callable[[str], list[T]]:
    def load(s: str) -> list[T]:
        return [serialization.loads(json.dumps(x), cls) for x in json.loads(s)]

    return load


# Screener rows are free-form dicts: tag Decimal/datetime so they survive the cache.
def _encode_row_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return {"$dec": str(v)}
    if isinstance(v, datetime):
        return {"$dt": v.astimezone(UTC).isoformat()}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    if isinstance(v, dict):
        return {str(k): _encode_row_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_encode_row_value(x) for x in v]
    return v


def _decode_row_hook(d: dict) -> Any:
    if len(d) == 1:
        (k, v), = d.items()
        if k == "$dec":
            return Decimal(v)
        if k == "$dt":
            return datetime.fromisoformat(v)
        if k == "$date":
            return date.fromisoformat(v)
    return d


def _dumps_rows(rows: list[dict]) -> str:
    return json.dumps(_encode_row_value(rows), sort_keys=True)


def _loads_rows(s: str) -> list[dict]:
    return json.loads(s, object_hook=_decode_row_hook)


def _utc(dt: datetime, name: str) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise _invalid(f"{name} must be a timezone-aware datetime")
    return dt.astimezone(UTC)


def _positive(value: int, name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise _invalid(f"{name} must be a positive integer", **{name: value})


_TF_DURATION = {
    Timeframe.m1: timedelta(minutes=1),
    Timeframe.m5: timedelta(minutes=5),
    Timeframe.m15: timedelta(minutes=15),
    Timeframe.h1: timedelta(hours=1),
    Timeframe.d1: timedelta(days=1),
    Timeframe.w1: timedelta(weeks=1),
}


def _batch_timeout(items: int) -> float:
    return max(PROVIDER_TIMEOUT_S, QUOTE_TIMEOUT_PER_ITEM_S * items)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _floor(dt: datetime, step: timedelta) -> datetime:
    """Floor a UTC datetime to a multiple of `step` since the epoch."""
    return dt - (dt - _EPOCH) % step


def _now() -> datetime:
    return datetime.now(UTC)


async def _closed_bars(
    provider: DataProvider, args: tuple, tf: Timeframe, market: str | None
) -> list[Bar]:
    """Drop still-forming candles; providers return raw bars. Closed = `bar_is_closed`:
    an exchange's 1d/1wk bar once its session has closed, otherwise ts + duration."""
    bars = await provider.get_bars(*args)
    now = _now()
    return [b for b in bars if bar_is_closed(market, tf, b.ts, now)]


def _bars_coverage(
    asset_class: AssetClass, tf: Timeframe, start: datetime, end: datetime
) -> Callable[[list[Bar]], tuple[bool, datetime]]:
    """(covers the range, rank) for a bars answer; a lower rank (earlier first bar) is wider.

    An empty answer starts "at end": it covers only a range shorter than the tolerance (a
    weekend for equities), so ranges with nothing in them do not trigger extra calls.
    """
    tolerance = BARS_COVERAGE_INTERVALS * _TF_DURATION[tf]
    if asset_class != AssetClass.crypto:
        tolerance += BARS_CLOSED_MARKET_SLACK

    def coverage(bars: list[Bar]) -> tuple[bool, datetime]:
        first = min((b.ts for b in bars), default=end)
        return first <= start + tolerance, first

    return coverage


class MarketDataRouter:
    def __init__(self, sessionmaker: async_sessionmaker, redis, http: httpx.AsyncClient):
        self._sm = sessionmaker
        self._redis = redis
        self._http = http
        self._instances: dict[str, tuple[DataProvider, float]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._clock = time.monotonic

    # ---------------------------------------------------------------- infrastructure

    async def _provider(self, session: AsyncSession, name: str, cls: type[DataProvider]):
        cached = self._instances.get(name)
        if (
            cached is not None
            and type(cached[0]) is cls
            and self._clock() - cached[1] < INSTANCE_MAX_AGE_S
        ):
            return cached[0]
        config = await get_connector_config(session, name)
        inst = cls(config, self._http)
        self._instances[name] = (inst, self._clock())
        return inst

    async def _slots(
        self, session: AsyncSession, asset_class: AssetClass, capability: Capability
    ) -> list[_Slot]:
        slots: list[_Slot] = []
        for name in await get_priorities(session, asset_class, capability):
            cls = PROVIDERS.get(name)
            if cls is None:
                continue
            conn = await session.get(Connector, name)
            if conn is None or not conn.enabled:
                continue
            rate = (
                conn.rate_limit_per_min
                if conn.rate_limit_per_min is not None
                else cls.default_rate_limit_per_min
            )
            if capability not in cls.capabilities:
                slots.append(_Slot(name, None, rate))
                continue
            try:
                provider = await self._provider(session, name, cls)
            except Exception as e:  # noqa: BLE001 - bad config/constructor: fall back
                slots.append(_Slot(name, None, rate, build_error=_describe(e)))
                continue
            slots.append(_Slot(name, provider, rate))
        return slots

    def _flight_done(self, key: str, task: asyncio.Task) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # mark retrieved even if every waiter was cancelled

    async def _single_flight(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Concurrent identical requests share one in-flight task."""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
            task.add_done_callback(lambda t, key=key: self._flight_done(key, t))
        # shield: a cancelled waiter must not cancel the work other waiters share
        return await asyncio.shield(task)

    async def _plan(
        self, asset_class: AssetClass, capability: Capability
    ) -> list[_Slot]:
        async with self._sm() as session:
            return await self._slots(session, asset_class, capability)

    async def _resolve(self, symbols: list[str]) -> dict[str, _Instr]:
        out: dict[str, _Instr] = {}
        async with self._sm() as session:
            for raw in symbols:
                sym = canonical_symbol(raw)
                if sym in out:
                    continue
                inst = await get_instrument(session, sym)
                out[sym] = _Instr(sym, inst.asset_class, dict(inst.provider_symbols or {}),
                                  inst.market)
        return out

    async def _cache_read(self, key: str) -> str | None:
        try:
            return await cache_get(self._redis, key)
        except Exception:
            log.warning("cache read failed for %s", key, exc_info=True)
            return None

    async def _cache_write(self, key: str, value: str, ttl: int) -> None:
        try:
            await cache_set(self._redis, key, value, ttl)
        except Exception:
            log.warning("cache write failed for %s", key, exc_info=True)

    async def _attempt(
        self,
        slot: _Slot,
        outcome: _Outcome,
        call: Callable[[DataProvider], Awaitable[Any]],
        memo: dict[str, Any] | None = None,
        batch: list | None = None,
    ) -> Any:
        """One provider call. Returns the result or _MISS (outcome annotated).

        `batch`, when given, is filled by `call` with the items it sends: the call then costs
        len(batch) rate-limit tokens and its timeout scales with the batch size.
        """
        outcome.attempted = True
        if slot.build_error is not None:
            outcome.errors[slot.name] = slot.build_error
            return _MISS
        if slot.provider is None:
            outcome.unsupported = True
            return _MISS
        if memo is not None and slot.name in memo:
            result = memo[slot.name]
            if isinstance(result, ProviderUnsupported):
                outcome.unsupported = True
                return _MISS
            if isinstance(result, str):
                outcome.errors[slot.name] = result
                return _MISS
            return result[0]
        result = await self._attempt_uncached(slot, outcome, call, batch)
        if memo is not None:
            if result is not _MISS:
                memo[slot.name] = (result,)
            elif slot.name in outcome.errors:
                memo[slot.name] = outcome.errors[slot.name]
            else:
                memo[slot.name] = ProviderUnsupported()
        return result

    async def _acquire(self, slot: _Slot, cost: int) -> bool:
        """Rate-limit gate. A Redis failure fails open: the call proceeds."""
        try:
            return await acquire(self._redis, slot.name, slot.rate_limit, cost=cost)
        except Exception:
            log.warning("rate limiter unavailable for %s; proceeding", slot.name, exc_info=True)
            return True

    async def _attempt_uncached(
        self,
        slot: _Slot,
        outcome: _Outcome,
        call: Callable[[DataProvider], Awaitable[Any]],
        batch: list | None,
    ) -> Any:
        assert slot.provider is not None
        coro = None
        try:
            coro = call(slot.provider)  # symbol mapping happens here (may be unsupported)
            size = len(batch) if batch else 1
            if not await self._acquire(slot, size):
                outcome.errors[slot.name] = RATE_LIMITED
                return _MISS
            awaitable, coro = coro, None
            timeout = (
                PROVIDER_TIMEOUT_S if batch is None else _batch_timeout(size)
            )
            return await asyncio.wait_for(awaitable, timeout)
        except ProviderUnsupported:
            outcome.unsupported = True
            return _MISS
        except Exception as e:  # noqa: BLE001 - recorded, reported via PROVIDER_UNAVAILABLE
            outcome.errors[slot.name] = _describe(e)
            return _MISS
        finally:
            if coro is not None and hasattr(coro, "close"):
                coro.close()

    async def _fetch(
        self,
        capability: Capability,
        asset_class: AssetClass,
        key_args: dict,
        ttl: int,
        call: Callable[[DataProvider], Awaitable[Any]],
        dumps: Callable[[Any], str],
        loads: Callable[[str], Any],
        outcome: _Outcome,
        memo: dict[str, Any] | None = None,
        coverage: Callable[[Any], tuple[bool, Any]] | None = None,
    ) -> Any:
        """Cache → providers in priority order → cache. Returns the result or _MISS.

        Identical concurrent fetches (same cache key) share one in-flight task; each caller
        merges the shared outcome into its own.

        `coverage`, when given, maps a result to (complete, rank): an incomplete result does
        not stop the walk; if no provider is complete, the lowest rank wins (ties: the earlier
        provider) and is what gets cached.
        """
        key = _cache_key(capability, {"asset_class": asset_class, **key_args})

        async def run() -> tuple[Any, _Outcome]:
            local = _Outcome()
            hit = await self._cache_read(key)
            if hit is not None:
                try:
                    return loads(hit), local
                except Exception:
                    log.warning("cache entry %s undecodable", key, exc_info=True)
            best: Any = _MISS
            best_rank: Any = None
            for slot in await self._plan(asset_class, capability):
                result = await self._attempt(slot, local, call, memo)
                if result is _MISS:
                    continue
                if coverage is not None:
                    complete, rank = coverage(result)
                    if not complete:
                        if best is _MISS or rank < best_rank:
                            best, best_rank = result, rank
                        continue
                await self._cache_write(key, dumps(result), ttl)
                return result, local
            if best is not _MISS:
                await self._cache_write(key, dumps(best), ttl)
            return best, local

        result, shared = await self._single_flight(key, run)
        outcome.merge(shared)
        return result

    async def _fetch_one(
        self, capability: Capability, asset_class: AssetClass, key_args: dict, ttl: int,
        call, dumps, loads, details: dict,
        coverage: Callable[[Any], tuple[bool, Any]] | None = None,
    ) -> Any:
        outcome = _Outcome()
        result = await self._fetch(
            capability, asset_class, key_args, ttl, call, dumps, loads, outcome,
            coverage=coverage,
        )
        if result is _MISS:
            raise outcome.error(capability, details)
        return result

    @staticmethod
    def _by_class(instrs: dict[str, _Instr]) -> dict[AssetClass, list[_Instr]]:
        groups: dict[AssetClass, list[_Instr]] = {}
        for i in instrs.values():
            groups.setdefault(i.asset_class, []).append(i)
        return groups

    @staticmethod
    def _map_items(
        p: DataProvider, instrs: list[_Instr]
    ) -> list[tuple[str, str]]:
        """(canonical, provider symbol) for the symbols this provider can map."""
        items = []
        for i in instrs:
            try:
                items.append((i.symbol, p.map_symbol(i.symbol, i.asset_class, i.override(p.name))))
            except ProviderUnsupported:
                continue
        if not items:
            raise ProviderUnsupported(f"{p.name}: no mappable symbols")
        return items

    # ---------------------------------------------------------------- quotes

    @_guard
    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict[str, Quote]:
        if not isinstance(max_age_s, int) or max_age_s < 0:
            raise _invalid("max_age_s must be a non-negative integer", max_age_s=max_age_s)
        if not symbols:
            return {}
        instrs = await self._resolve(symbols)
        key = f"quotes:{max_age_s}:" + ",".join(sorted(instrs))
        result = await self._single_flight(key, lambda: self._quotes(instrs, max_age_s))
        return {s: result[s] for s in instrs}

    @_guard
    async def probe_quote(
        self, symbol: str, asset_class: AssetClass, provider_symbols: dict | None = None
    ) -> Quote:
        """A fresh quote for a symbol that need not be an instrument yet (on-demand
        discovery): same providers and fallback as get_quotes, without the catalog lookup."""
        sym = canonical_symbol(symbol)
        instr = _Instr(sym, AssetClass(asset_class), dict(provider_symbols or {}))
        return (await self._quotes({sym: instr}, 0))[sym]

    async def _fresh_cached_quote(self, sym: str, max_age_s: int, now: datetime) -> Quote | None:
        if max_age_s == 0:  # explicit refresh
            return None
        try:
            meta = await read_quote_meta(self._redis, sym)
        except Exception:
            log.warning("quote cache read failed for %s", sym, exc_info=True)
            return None
        if meta is None:
            return None
        q, fetched_at = meta
        # an off-hours quote keeps an old ts, but was confirmed current at fetched_at
        if (now - max(q.ts, fetched_at)).total_seconds() <= max_age_s:
            return q
        return None

    async def _quotes(self, instrs: dict[str, _Instr], max_age_s: int) -> dict[str, Quote]:
        result: dict[str, Quote] = {}
        now = datetime.now(UTC)
        for sym in instrs:
            q = await self._fresh_cached_quote(sym, max_age_s, now)
            if q is not None:
                result[sym] = q

        pending_all = {s: i for s, i in instrs.items() if s not in result}
        for asset_class, group in self._by_class(pending_all).items():
            outcome = _Outcome()  # per asset class: errors never mix across groups
            pending = {i.symbol: i for i in group}
            for slot in await self._plan(asset_class, Capability.quote):
                if not pending:
                    break
                todo = list(pending.values())
                sent: list[tuple[str, str]] = []

                def call(p: DataProvider, todo=todo, sent=sent):
                    sent[:] = self._map_items(p, todo)
                    return p.get_quotes(list(sent))

                got = await self._attempt(slot, outcome, call, batch=sent)
                if got is _MISS:
                    continue
                for sym in list(pending):
                    q = got.get(sym)
                    if q is None:
                        continue
                    if q.symbol != sym:
                        q = replace(q, symbol=sym)
                    result[sym] = q
                    del pending[sym]
                    try:
                        await write_quote(self._redis, q)
                    except Exception:
                        log.warning("quote cache write failed for %s", sym, exc_info=True)
                missing = sorted({s for s, _ in sent} & set(pending))
                if missing:  # mappable, yet absent from the answer: a provider failure
                    outcome.errors[slot.name] = "missing: " + ",".join(missing)
                if len(pending) > len(missing):  # the rest it cannot map
                    outcome.unsupported = True
            if pending:  # stop at the first failing group
                raise outcome.error(
                    Capability.quote, {"asset_class": str(asset_class), "symbols": list(pending)}
                )
        return result

    # ---------------------------------------------------------------- single symbol

    @_guard
    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise _invalid("unknown timeframe", timeframe=str(timeframe)) from None
        start, end = _utc(start, "start"), _utc(end, "end")
        if start >= end:
            raise _invalid("start must be before end")
        instr = (await self._resolve([symbol]))[canonical_symbol(symbol)]
        # floor the range so requests made moments apart share one cache entry
        step = min(_TF_DURATION[tf], BARS_FLOOR_MAX)
        start, end = _floor(start, step), _floor(end, step)
        if start >= end:  # the whole range sits inside one step: no bar can start in it
            return []

        def call(p: DataProvider):
            ps = p.map_symbol(instr.symbol, instr.asset_class, instr.override(p.name))
            return _closed_bars(p, (instr.symbol, ps, tf, start, end), tf, instr.market)

        # a cached list is final up to its fetch: it expires when the next bar closes
        return await self._fetch_one(
            Capability.bars, instr.asset_class,
            {"symbol": instr.symbol, "timeframe": tf, "start": start, "end": end},
            bars_cache_ttl(instr.market, tf, _now()), call, _dumps_list, _loads_list(Bar), {"symbol": instr.symbol},
            coverage=_bars_coverage(instr.asset_class, tf, start, end),
        )

    @_guard
    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        instr = (await self._resolve([symbol]))[canonical_symbol(symbol)]

        def call(p: DataProvider):
            ps = p.map_symbol(instr.symbol, instr.asset_class, instr.override(p.name))
            return p.get_fundamentals(instr.symbol, ps)

        return await self._fetch_one(
            Capability.fundamentals, instr.asset_class, {"symbol": instr.symbol},
            CACHE_TTL["fundamentals"], call, serialization.dumps,
            lambda s: serialization.loads(s, Fundamentals), {"symbol": instr.symbol},
        )

    # ---------------------------------------------------------------- multi symbol

    async def _per_group(
        self, capability: Capability, symbols: list[str], key_args: dict, ttl: int,
        make_call, dumps, loads,
        coverage: Callable[[Any], tuple[bool, Any]] | None = None,
    ) -> list:
        """Fetch per asset class and merge. A class with no enabled provider for the
        capability (nothing attempted) contributes nothing; a real failure raises."""
        instrs = await self._resolve(symbols)
        merged: list = []
        for asset_class, group in self._by_class(instrs).items():
            syms = sorted(i.symbol for i in group)
            outcome = _Outcome()
            got = await self._fetch(
                capability, asset_class, {"symbols": syms, **key_args}, ttl,
                make_call(group), dumps, loads, outcome, coverage=coverage,
            )
            if got is _MISS:
                if not outcome.attempted:
                    continue
                raise outcome.error(capability, {"symbols": syms})
            merged.extend(got)
        return merged

    @_guard
    async def get_news(self, symbols: list[str], limit: int = 20) -> list[NewsItem]:
        _positive(limit, "limit")
        if not symbols:
            return []
        items = await self._per_group(
            Capability.news, symbols, {"limit": limit}, CACHE_TTL["news"],
            lambda group: lambda p: p.get_news(self._map_items(p, group), limit),
            _dumps_list, _loads_list(NewsItem),
            # an empty answer is not final: ask the next provider (all empty -> empty)
            coverage=lambda result: (bool(result), 0),
        )
        seen: set[str] = set()
        unique = []
        for n in sorted(items, key=lambda n: n.ts, reverse=True):
            if n.id not in seen:
                seen.add(n.id)
                unique.append(n)
        return unique[:limit]

    @_guard
    async def get_calendar(
        self, symbols: list[str], start: date, end: date
    ) -> list[CalendarEvent]:
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise _invalid("start must be a date not after end")
        if not symbols:
            return []
        events = await self._per_group(
            Capability.calendar, symbols, {"start": start, "end": end},
            CACHE_TTL["calendar"],
            lambda group: lambda p: p.get_calendar(self._map_items(p, group), start, end),
            _dumps_list, _loads_list(CalendarEvent),
        )
        return sorted(events, key=lambda e: (e.date, e.symbol, e.kind))

    # ---------------------------------------------------------------- fx

    @_guard
    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        if not isinstance(base, str) or not isinstance(quote, str):
            raise _invalid("currencies must be strings")
        base, quote = base.strip().upper(), quote.strip().upper()
        if not base or not quote:
            raise _invalid("currencies must not be empty")
        if base == quote:
            return Decimal(1)
        return await self._fetch_one(
            Capability.fx, AssetClass.forex, {"base": base, "quote": quote},
            CACHE_TTL["fx"], lambda p: p.get_fx_rate(base, quote), str, Decimal,
            {"base": base, "quote": quote},
        )

    @_guard
    async def get_fx_rate_on(self, base: str, quote: str, day: date) -> Decimal:
        """The reference rate of `day` (providers with dated rates: Frankfurter, the ECB
        rate, the previous business day's on a weekend or holiday); cached a day once the
        day is over (a rate of today may still change)."""
        if not isinstance(base, str) or not isinstance(quote, str):
            raise _invalid("currencies must be strings")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise _invalid("day must be a date")
        base, quote = base.strip().upper(), quote.strip().upper()
        if not base or not quote:
            raise _invalid("currencies must not be empty")
        if base == quote:
            return Decimal(1)
        ttl = CACHE_TTL["fx"] if day >= _now().date() else 86400
        return await self._fetch_one(
            Capability.fx, AssetClass.forex,
            {"base": base, "quote": quote, "day": day.isoformat()}, ttl,
            lambda p: p.get_fx_rate_on(base, quote, day), str, Decimal,
            {"base": base, "quote": quote, "day": day.isoformat()},
        )

    # ---------------------------------------------------------------- all classes

    async def _across_classes(
        self, capability: Capability, classes: list[AssetClass], key_args: dict, ttl: int,
        call, dumps, loads, details: dict,
    ) -> list:
        """Run per asset class, merge successes; fail only when no class succeeded."""
        outcome = _Outcome()
        memo: dict[str, Any] = {}  # one call per provider even if it serves many classes
        merged: list = []
        succeeded = False
        for ac in classes:
            got = await self._fetch(
                capability, ac, key_args, ttl, call, dumps, loads, outcome, memo
            )
            if got is not _MISS:
                succeeded = True
                merged.extend(got)
        if not succeeded:
            raise outcome.error(capability, details)
        return merged

    @_guard
    async def search(self, query: str, limit: int = 20) -> list[InstrumentHit]:
        _positive(limit, "limit")
        if not isinstance(query, str) or not query.strip():
            raise _invalid("query must not be empty")
        q = query.strip()
        hits = await self._across_classes(
            Capability.search, list(AssetClass), {"query": q, "limit": limit},
            CACHE_TTL["search"], lambda p: p.search(q, limit),
            _dumps_list, _loads_list(InstrumentHit), {"query": q},
        )
        seen: set[str] = set()
        unique = []
        for h in hits:
            if h.symbol not in seen:
                seen.add(h.symbol)
                unique.append(h)
        return unique[:limit]

    @_guard
    async def screen(self, f: ScreenFilter) -> list[dict]:
        if not isinstance(f, ScreenFilter):
            raise _invalid("filter must be a ScreenFilter")
        _positive(f.limit, "limit")
        classes = [f.asset_class] if f.asset_class is not None else list(AssetClass)
        rows = await self._across_classes(
            Capability.screener, classes, {"filter": f}, CACHE_TTL["screener"],
            lambda p: p.screen(f), _dumps_rows, _loads_rows, {},
        )
        seen: set = set()
        unique = []
        for r in rows:
            key = r.get("symbol")
            if key is not None and key in seen:
                continue
            seen.add(key)
            unique.append(r)
        return unique[: f.limit]

    # ---------------------------------------------------------------- streaming / health

    @_guard
    async def provider_for_stream(self, asset_class: AssetClass) -> DataProvider | None:
        for slot in await self._plan(AssetClass(asset_class), Capability.stream):
            if slot.provider is not None:
                return slot.provider
        return None

    async def _health(
        self, name: str, provider: DataProvider
    ) -> tuple[str, bool | None, str | None]:
        """(name, ok, error); ok is None when the provider has no health check."""
        try:
            await asyncio.wait_for(provider.health_check(), PROVIDER_TIMEOUT_S)
            return name, True, None
        except ProviderUnsupported:
            return name, None, None
        except Exception as e:  # noqa: BLE001 - recorded via record_health
            return name, False, _describe(e)

    @_guard
    async def check_health(self) -> dict[str, bool]:
        """Health-check every enabled, registered connector and record the outcome.

        Providers that do not implement health_check are left untouched.
        """
        targets: list[tuple[str, DataProvider]] = []
        build_failures: list[tuple[str, bool | None, str | None]] = []
        async with self._sm() as session:
            for c in await list_connectors(session):
                cls = PROVIDERS.get(c.name)
                if not c.enabled or cls is None:
                    continue
                try:
                    targets.append((c.name, await self._provider(session, c.name, cls)))
                except Exception as e:  # noqa: BLE001 - recorded via record_health
                    build_failures.append((c.name, False, _describe(e)))
        checks = await asyncio.gather(*(self._health(n, p) for n, p in targets))
        results = build_failures + [(n, ok, err) for n, ok, err in checks if ok is not None]
        async with self._sm() as session:
            for name, ok, err in results:
                await record_health(session, name, ok, err)
            await session.commit()
        return {name: ok for name, ok, _ in results}
