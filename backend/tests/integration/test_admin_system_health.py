"""`GET /api/system-health` («Salute del sistema»): admin and viewer read it, it is cached
30 s, and it aggregates the stored state (worker heartbeat and jobs, connectors, quotes,
Paperclip reader and cycles, watchdog warnings, Telegram outbox, costs, MCP errors)."""
# ruff: noqa: F811  (the `admin` fixture of test_admin_telegram, imported then requested)

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from redis.asyncio import Redis
from test_admin_telegram import admin  # noqa: F401  (the fixture)

from smtrading.admin import routes_system_health
from smtrading.admin.auth import hash_password
from smtrading.admin.models import AdminUser
from smtrading.agents.models import McpAuditLog
from smtrading.common.config import get_settings
from smtrading.common.heartbeat import HEARTBEAT_KEY
from smtrading.paperclip.models import AgentCostDay
from smtrading.paperclip.watchdog import PREFIX
from smtrading.telegram.models import TelegramOutbox
from smtrading.worker.jobstate import JOBS_KEY, WORKER_FIELD


@pytest.fixture(autouse=True)
def _no_cache():
    routes_system_health.clear_cache()
    yield
    routes_system_health.clear_cache()


async def _viewer(admin) -> httpx.AsyncClient:
    email = "viewer-health@example.com"
    async with admin.sm() as s:
        s.add(AdminUser(email=email, password_hash=await hash_password("pw-viewer-1"),
                        role="viewer"))
        await s.commit()
    c = httpx.AsyncClient(transport=admin.client._transport, base_url="http://test")
    r = await c.post("/api/auth/login", json={"email": email, "password": "pw-viewer-1"})
    assert r.status_code == 200, r.text
    return c


async def _redis(admin) -> Redis:
    return Redis.from_url(admin.redis_url)


async def test_admin_and_viewer_read_the_health(admin):
    r = await admin.client.get("/api/system-health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["sections"]) == {"services", "providers", "quotes", "paperclip",
                                     "cycles", "watchdog", "telegram", "costs", "mcp",
                                     "backup"}
    # no worker heartbeat in the test: the services are a problem, and the reason says so
    assert body["status"] == "problema"
    assert "worker fermo: nessun battito" in body["reasons"]
    assert body["sections"]["paperclip"]["status"] == "spento"
    assert body["version"]["build"]
    viewer = await _viewer(admin)
    try:
        r = await viewer.get("/api/system-health")
        assert r.status_code == 200
    finally:
        await viewer.aclose()


async def test_health_aggregates_the_stored_state(admin):
    redis = await _redis(admin)
    now = time.time()
    try:
        await redis.set(HEARTBEAT_KEY, str(now))
        await redis.delete(JOBS_KEY)
        await redis.hset(JOBS_KEY, WORKER_FIELD, json.dumps({"started_at": now - 600}))
        await redis.hset(JOBS_KEY, "broker sync", json.dumps({
            "state": "restarting", "started_at": now - 10, "crashes": 1,
            "last_crash_at": now - 5, "last_error": "RuntimeError: boom"}))
        zone = ZoneInfo(get_settings().tz)
        today = datetime.now(zone).date()
        async with admin.sm() as s:
            s.add(AgentCostDay(day=today, paperclip_agent_id="pa1", paperclip_agent_name="T",
                               api_usd=Decimal("1.5"), subscription_usd=Decimal(3),
                               other_usd=Decimal("0.5"), runs=4))
            s.add(TelegramOutbox(source="system", category="system", level="warning",
                                 text=f"{PREFIX}AGENDA mancante", status="sent"))
            s.add(TelegramOutbox(source="system", category="system", level="info",
                                 text="x", status="failed"))
            s.add(McpAuditLog(agent_id=None, method="get_quote", params={},
                              outcome="INTERNAL", latency_ms=1))
            s.add(McpAuditLog(agent_id=None, method="get_quote", params={}, outcome="ok",
                              latency_ms=1))
            await s.commit()
        r = await admin.client.get("/api/system-health")
        body = r.json()
        services = body["sections"]["services"]
        assert services["status"] == "problema"
        assert services["jobs"][0]["name"] == "broker sync"
        assert services["jobs"][0]["last_error"] == "RuntimeError: boom"
        assert body["version"]["worker_started_at"] is not None
        assert body["sections"]["costs"]["today_usd"] == "2"  # billed: api + other
        # the watchdog section follows the Paperclip reader (off here)
        assert body["sections"]["watchdog"]["status"] == "spento"
        mcp = body["sections"]["mcp"]
        assert (mcp["calls"], mcp["errors"], mcp["internal"]) == (2, 1, 1)
        telegram = body["sections"]["telegram"]
        assert telegram["failed_24h"] == 1

        # cached: a change is not seen within 30 s
        await redis.delete(JOBS_KEY)
        again = (await admin.client.get("/api/system-health")).json()
        assert again["checked_at"] == body["checked_at"]
        routes_system_health.clear_cache()
        fresh = (await admin.client.get("/api/system-health")).json()
        assert fresh["sections"]["services"]["jobs"] == []
        assert fresh["checked_at"] >= body["checked_at"]
    finally:
        await redis.delete(JOBS_KEY, HEARTBEAT_KEY)
        await redis.aclose()


async def test_outbox_oldest_pending_age(admin):
    async with admin.sm() as s:
        s.add(TelegramOutbox(source="system", category="system", level="info", text="old",
                             status="pending",
                             created_at=datetime.now(UTC) - timedelta(minutes=30)))
        await s.commit()
    body = (await admin.client.get("/api/system-health")).json()
    t = body["sections"]["telegram"]
    assert t["pending"] == 1 and t["oldest_pending_age_s"] >= 1700
