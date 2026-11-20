"""The suspect price alarm (plan 25): Telegram once per symbol and day, only for symbols
with open orders or positions whose quotes stay unconfirmed more than 15 minutes."""

import time

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.marketdata import quality
from smtrading.marketdata.streaming import track
from smtrading.marketdata.suspect_alarm import SuspectAlarm
from smtrading.telegram.models import TelegramOutbox


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    await redis.flushdb()
    yield sm, redis
    await redis.aclose()
    await engine.dispose()


async def _texts(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text).order_by(TelegramOutbox.id)))


async def test_alarm_once_per_symbol_and_day(env):
    sm, redis = env
    now = [time.time()]
    alarm = SuspectAlarm(sm, redis, clock=lambda: now[0])
    await track(redis, ["NVDA"], source="positions")
    await track(redis, ["AAPL"], source="orders")
    await track(redis, ["MSFT"], source="watchlists")
    for sym in ("NVDA", "AAPL", "MSFT"):
        await redis.set(quality.SINCE_KEY.format(sym), str(now[0] - 10 * 60))

    assert await alarm.tick() == []  # 10 minutes: not yet
    now[0] += 6 * 60
    assert await alarm.tick() == ["AAPL", "NVDA"]  # never the watchlist-only symbol
    texts = await _texts(sm)
    assert len(texts) == 2 and "NVDA" in texts[1] and "16 min" in texts[1]
    assert "Prezzo non affidabile" in texts[0]
    now[0] += 60
    assert await alarm.tick() == []  # once a day


async def test_alarm_quiet_once_the_run_ends(env):
    sm, redis = env
    now = time.time()
    await track(redis, ["NVDA"], source="orders")
    await redis.set(quality.SINCE_KEY.format("NVDA"), str(now - 20 * 60))
    await redis.delete(quality.SINCE_KEY.format("NVDA"))  # a good quote arrived
    assert await SuspectAlarm(sm, redis, clock=lambda: now).tick() == []
    assert await _texts(sm) == []
