import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import redis.asyncio as aioredis

from smtrading.marketdata.cache import (
    CACHE_TTL,
    bars_ttl,
    cache_get,
    cache_set,
    read_quote,
    write_quote,
)
from smtrading.marketdata.ratelimit import acquire
from smtrading.marketdata.serialization import loads_quote
from smtrading.marketdata.types import Quote, Timeframe


@pytest.fixture
async def r(redis_url):
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


async def test_token_bucket(r):
    assert [await acquire(r, "p", 2, now=1000.0) for _ in range(3)] == [True, True, False]
    assert await acquire(r, "p", 2, now=1030.0) is True
    assert await r.ttl("rl:p") > 0


async def test_token_bucket_capacity_cap_and_zero(r):
    assert await acquire(r, "q", 1, now=0.0) is True
    # a long idle period never accumulates more than `capacity` tokens
    assert [await acquire(r, "q", 1, now=10_000.0) for _ in range(2)] == [True, False]
    assert await acquire(r, "z", 0, now=0.0) is False
    assert await acquire(r, "z", -5, now=0.0) is False


async def test_token_bucket_backwards_clock(r):
    assert await acquire(r, "m", 1, now=1000.0) is True
    assert await acquire(r, "m", 1, now=990.0) is False
    assert await acquire(r, "m", 1, now=1000.0) is False
    assert await acquire(r, "m", 1, now=1060.0) is True


async def test_token_bucket_fractional_refill(r):
    assert [await acquire(r, "f", 2, now=500.0) for _ in range(2)] == [True, True]
    assert await acquire(r, "f", 2, now=515.0) is False
    assert await acquire(r, "f", 2, now=530.0) is True


async def test_token_bucket_cost(r):
    # capacity 10: a 4-token batch twice, then a 4-token batch is refused (2 left) without
    # consuming anything, and a 2-token one still fits
    assert await acquire(r, "c", 10, cost=4, now=0.0) is True
    assert await acquire(r, "c", 10, cost=4, now=0.0) is True
    assert await acquire(r, "c", 10, cost=4, now=0.0) is False
    assert await acquire(r, "c", 10, cost=2, now=0.0) is True
    assert await acquire(r, "c", 10, now=0.0) is False
    # a batch larger than the capacity is capped: a full bucket admits it once
    assert await acquire(r, "big", 3, cost=50, now=0.0) is True
    assert await acquire(r, "big", 3, now=0.0) is False


async def test_token_bucket_default_now(r):
    assert await acquire(r, "t", 60) is True


async def test_quote_roundtrip_and_publish(r):
    q = Quote(
        "BTC-USD", Decimal("99.5"), Decimal("100.5"), None,
        datetime(2026, 1, 1, 12, 0, tzinfo=UTC), "kraken",
    )
    assert await read_quote(r, "BTC-USD") is None
    pubsub = r.pubsub()
    await pubsub.subscribe("ticks:BTC-USD")
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    await write_quote(r, q)
    assert await read_quote(r, "BTC-USD") == q
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg:
            break
    assert msg is not None
    assert loads_quote(msg["data"]) == q
    await pubsub.aclose()


async def test_cache_set_get_ttl(r):
    assert await cache_get(r, "k") is None
    await cache_set(r, "k", "v", 1)
    assert await cache_get(r, "k") == "v"
    await asyncio.sleep(1.2)
    assert await cache_get(r, "k") is None


def test_ttl_constants():
    assert CACHE_TTL == {
        "quote": 5, "news": 300, "fundamentals": 86400, "calendar": 21600,
        "fx": 60, "search": 86400, "screener": 900,
    }
    assert [bars_ttl(t) for t in Timeframe] == [60, 300, 900, 3600, 21600, 21600]
