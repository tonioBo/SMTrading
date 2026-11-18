from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smtrading.marketdata import serialization
from smtrading.marketdata.types import Quote, Timeframe

CACHE_TTL: dict[str, int] = {
    "quote": 5,
    "extended": 60,
    "news": 300,
    "fundamentals": 86400,
    "calendar": 21600,
    "fx": 60,
    "search": 86400,
    "screener": 900,
}

_BARS_TTL: dict[Timeframe, int] = {
    Timeframe.m1: 60,
    Timeframe.m5: 300,
    Timeframe.m15: 900,
    Timeframe.h1: 3600,
    Timeframe.d1: 21600,
    Timeframe.w1: 21600,
}


def bars_ttl(tf: Timeframe) -> int:
    return _BARS_TTL[tf]


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


async def cache_get(redis, key: str) -> str | None:
    v = await redis.get(key)
    return None if v is None else _s(v)


async def cache_set(redis, key: str, value: str, ttl_s: int) -> None:
    await redis.set(key, value, ex=ttl_s)


def _opt(v: Decimal | None) -> str:
    return "" if v is None else str(v)


# Atomic "newer ts wins": skip HSET+PUBLISH when the stored quote is strictly newer.
# ts_us (integer microseconds since epoch) is compared numerically; entries written before
# the guard existed have no ts_us and are always overwritten.
_WRITE_QUOTE_LUA = """
local cur = redis.call('HGET', KEYS[1], 'ts_us')
if cur and tonumber(cur) > tonumber(ARGV[1]) then
  return 0
end
redis.call('HSET', KEYS[1], unpack(ARGV, 3))
redis.call('PUBLISH', KEYS[2], ARGV[2])
return 1
"""


# a source ts further in the future than this is clamped for the newer-wins comparison,
# so one bad future timestamp cannot freeze a symbol
MAX_FUTURE_SKEW = timedelta(seconds=5)


def _ts_us(ts: datetime) -> int:
    return int(ts.astimezone(UTC).timestamp() * 1_000_000)


async def write_quote(redis, q: Quote, *, fetched_at: datetime | None = None) -> bool:
    """Store and publish the latest quote unless the cached one is newer (by event `ts`).

    `fetched_at` (default now, UTC) is when we received it. Returns True when written.
    The comparison key `ts_us` is clamped to now + MAX_FUTURE_SKEW (the stored `ts` is the
    source's, unchanged).
    """
    fetched = (fetched_at or datetime.now(UTC)).astimezone(UTC)
    ts_us = min(_ts_us(q.ts), _ts_us(datetime.now(UTC) + MAX_FUTURE_SKEW))
    fields = {
        "bid": _opt(q.bid),
        "ask": _opt(q.ask),
        "last": _opt(q.last),
        "ts": q.ts.isoformat(),
        "ts_us": str(ts_us),
        "provider": q.provider,
        "fetched_at": fetched.isoformat(),
    }
    flat = [x for kv in fields.items() for x in kv]
    script = redis.register_script(_WRITE_QUOTE_LUA)
    done = await script(
        keys=[f"quote:{q.symbol}", f"ticks:{q.symbol}"],
        args=[str(ts_us), serialization.dumps(q), *flat],
    )
    return bool(done)


async def read_quote(redis, symbol: str) -> Quote | None:
    meta = await read_quote_meta(redis, symbol)
    return None if meta is None else meta[0]


async def read_quote_meta(redis, symbol: str) -> tuple[Quote, datetime] | None:
    """(quote, fetched_at). Entries written without fetched_at fall back to the quote ts."""
    raw = await redis.hgetall(f"quote:{symbol}")
    if not raw:
        return None
    h = {_s(k): _s(v) for k, v in raw.items()}

    def dec(k: str) -> Decimal | None:
        return Decimal(h[k]) if h.get(k) else None

    q = Quote(
        symbol=symbol,
        bid=dec("bid"),
        ask=dec("ask"),
        last=dec("last"),
        ts=datetime.fromisoformat(h["ts"]),
        provider=h["provider"],
    )
    fetched = datetime.fromisoformat(h["fetched_at"]) if h.get("fetched_at") else q.ts
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return q, fetched.astimezone(UTC)
