"""1-minute bars published to a Redis stream `bars1m:{symbol}` (intraday mode B)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import redis.asyncio as aioredis

from smtrading.marketdata.minute_bars import (
    BARS_KEY,
    BARS_MAXLEN,
    StreamBar,
    publish_minute_bar,
    read_minute_bars,
)
from smtrading.marketdata.types import Bar

T0 = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)


def _bar(minute: int, close: str = "1.5") -> Bar:
    return Bar(ts=T0 + timedelta(minutes=minute), open=Decimal(1), high=Decimal(2),
               low=Decimal("0.5"), close=Decimal(close), volume=Decimal("100.25"))


@pytest.fixture
async def redis(redis_url):
    r = aioredis.from_url(redis_url, decode_responses=True)
    await r.flushdb()
    yield r
    await r.aclose()


def test_constants():
    assert BARS_KEY.format(symbol="AAPL") == "bars1m:AAPL" and BARS_MAXLEN == 1000


async def test_publish_and_read(redis):
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(0)))
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(1, "1.75")))
    [(_, e)] = await redis.xrange("bars1m:AAPL", count=1)
    assert e == {"ts": T0.isoformat(), "o": "1", "h": "2", "l": "0.5", "c": "1.5",
                 "v": "100.25"}
    got = await read_minute_bars(redis, "AAPL")
    assert [b for _, b in got] == [_bar(0), _bar(1, "1.75")]
    assert all(isinstance(b.close, Decimal) for _, b in got)
    after_first = await read_minute_bars(redis, "AAPL", since_id=got[0][0])
    assert after_first == got[1:]
    assert await read_minute_bars(redis, "AAPL", count=1) == got[:1]
    assert await read_minute_bars(redis, "MSFT") == []


async def test_same_ts_as_last_entry_adds_nothing(redis):
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(0)))
    await publish_minute_bar(redis, StreamBar("AAPL", _bar(0, "9")))
    assert await redis.xlen("bars1m:AAPL") == 1


async def test_stream_trimmed(redis):
    for i in range(1200):
        await publish_minute_bar(redis, StreamBar("AAPL", _bar(i)))
    assert 1000 <= await redis.xlen("bars1m:AAPL") < 1200
