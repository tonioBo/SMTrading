from datetime import UTC, datetime
from decimal import Decimal

from smtrading.marketdata import serialization
from smtrading.marketdata.types import Quote, Timeframe

CACHE_TTL: dict[str, int] = {
    "quote": 5,
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


async def write_quote(redis, q: Quote, *, fetched_at: datetime | None = None) -> None:
    """Store the latest quote; `fetched_at` (default now, UTC) is when we received it."""
    fetched = (fetched_at or datetime.now(UTC)).astimezone(UTC)
    await redis.hset(
        f"quote:{q.symbol}",
        mapping={
            "bid": _opt(q.bid),
            "ask": _opt(q.ask),
            "last": _opt(q.last),
            "ts": q.ts.isoformat(),
            "provider": q.provider,
            "fetched_at": fetched.isoformat(),
        },
    )
    await redis.publish(f"ticks:{q.symbol}", serialization.dumps(q))


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
