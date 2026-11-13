import time

import pytest
from redis.asyncio import Redis

from smtrading.common.heartbeat import HEARTBEAT_KEY


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


async def test_build_header_on_api_responses(pg_url, redis_url, monkeypatch):
    """I4: every API response names the build so a stale SPA can tell it must reload."""
    import httpx

    from smtrading.common.config import get_settings

    for k, v in {"SMT_DATABASE_URL": pg_url, "SMT_REDIS_URL": redis_url,
                 "SMT_MASTER_KEY": "k", "SMT_ADMIN_EMAIL": "a@example.com",
                 "SMT_ADMIN_PASSWORD": "p", "SMT_BUILD_ID": "abc123"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    from smtrading.api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/auth/me")
        assert r.status_code == 401 and r.headers["X-SMT-Build"] == "abc123"
        assert (await client.get("/health")).headers["X-SMT-Build"] == "abc123"
