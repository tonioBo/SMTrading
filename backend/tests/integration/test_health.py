import time

import pytest
from redis.asyncio import Redis

from smtrading.api.main import HEARTBEAT_KEY


@pytest.fixture
async def redis_client(redis_url):
    client = Redis.from_url(redis_url)
    await client.delete(HEARTBEAT_KEY)
    yield client
    await client.delete(HEARTBEAT_KEY)
    await client.aclose()


async def test_health_degraded_without_heartbeat(app_client, redis_client):
    r = await app_client.get("/health")
    assert r.status_code == 503
    assert r.json() == {
        "status": "degraded", "db": True, "redis": True, "worker_heartbeat_age_s": None,
    }


async def test_health_ok_with_fresh_heartbeat(app_client, redis_client):
    await redis_client.set(HEARTBEAT_KEY, str(time.time()))
    r = await app_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] is True and body["redis"] is True
    assert 0 <= body["worker_heartbeat_age_s"] < 30
