from datetime import datetime
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


async def write_quote(redis, q: Quote) -> None:
    await redis.hset(
        f"quote:{q.symbol}",
        mapping={
            "bid": _opt(q.bid),
            "ask": _opt(q.ask),
            "last": _opt(q.last),
            "ts": q.ts.isoformat(),
            "provider": q.provider,
        },
    )
    await redis.publish(f"ticks:{q.symbol}", serialization.dumps(q))


async def read_quote(redis, symbol: str) -> Quote | None:
    raw = await redis.hgetall(f"quote:{symbol}")
    if not raw:
        return None
    h = {_s(k): _s(v) for k, v in raw.items()}

    def dec(k: str) -> Decimal | None:
        return Decimal(h[k]) if h.get(k) else None

    return Quote(
        symbol=symbol,
        bid=dec("bid"),
        ask=dec("ask"),
        last=dec("last"),
        ts=datetime.fromisoformat(h["ts"]),
        provider=h["provider"],
    )
