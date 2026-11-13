"""Admin API: an agent's watchlists and alerts (ownership, untrack only when
unreferenced, audit row and agent notification)."""

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.models import McpAuditLog
from smtrading.agents.service import create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.events import dispatch_post_commit
from smtrading.marketdata.streaming import TRACKED_KEY
from smtrading.risk.models import Notification
from smtrading.watch import service as watch
from smtrading.watch.models import AlertCondition

D = Decimal
PASSWORD = "correct-horse-battery"


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("200.00"), D("200.10"), D("200.05"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.qs, ns.app = client, sm, qs, app
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()



async def _commit(sm, redis_url, fn):
    redis = Redis.from_url(redis_url)
    try:
        async with sm() as s:
            out = await fn(s)
            await s.commit()
            await dispatch_post_commit(s, redis)
        return out
    finally:
        await redis.aclose()


async def _tracked(redis_url, source: str) -> set[str]:
    redis = Redis.from_url(redis_url)
    try:
        return {m.decode() for m in await redis.smembers(f"{TRACKED_KEY}:{source}")}
    finally:
        await redis.aclose()


@pytest.fixture
async def book(admin, redis_url):
    sm = admin.sm

    async def make_agent(s):
        a, _ = await create_agent(s, f"w-{uuid.uuid4().hex[:6]}")
        return a.id

    aid = await _commit(sm, redis_url, make_agent)
    bid = await _commit(sm, redis_url, make_agent)
    tech = await _commit(sm, redis_url, lambda s: watch.create_watchlist(s, aid, "tech"))
    other = await _commit(sm, redis_url, lambda s: watch.create_watchlist(s, aid, "other"))
    await _commit(sm, redis_url, lambda s: watch.add_to_watchlist(
        s, aid, tech["id"], ["AAPL", "MSFT"]))
    await _commit(sm, redis_url, lambda s: watch.add_to_watchlist(s, aid, other["id"], ["AAPL"]))
    alert = await _commit(sm, redis_url, lambda s: watch.create_alert(
        s, admin.qs, aid, "AAPL", AlertCondition.price_above, D(300)))
    return {"aid": aid, "bid": bid, "tech": tech["id"], "other": other["id"],
            "alert": alert.id}


async def _notes(sm, agent_id) -> list[dict]:
    async with sm() as s:
        return [n.payload for n in await s.scalars(
            select(Notification).where(Notification.agent_id == str(agent_id),
                                       Notification.kind == "admin.watch_changed")
            .order_by(Notification.id))]


async def _audit(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(
            select(McpAuditLog.method).where(McpAuditLog.method.like("admin:%.%"))
            .where(McpAuditLog.method.like("admin:watchlist.%")
                   | McpAuditLog.method.like("admin:alert.%"))
            .order_by(McpAuditLog.id)))


async def test_lists_watchlists_and_alerts(admin, book):
    c = admin.client
    r = await c.get(f"/api/agents/{book['aid']}/watchlists")
    assert r.status_code == 200
    assert {w["name"]: w["symbols"] for w in r.json()} == {"tech": ["AAPL", "MSFT"],
                                                           "other": ["AAPL"]}
    r = await c.get(f"/api/agents/{book['aid']}/alerts")
    assert [(a["symbol"], a["status"]) for a in r.json()] == [("AAPL", "active")]
    assert (await c.get(f"/api/agents/{book['bid']}/watchlists")).json() == []
    assert (await c.get(f"/api/agents/{uuid.uuid4()}/watchlists")).status_code == 404


async def test_ownership_is_not_found(admin, book):
    c, b = admin.client, book["bid"]
    r = await c.delete(f"/api/agents/{b}/watchlists/{book['tech']}/items/AAPL")
    assert r.status_code == 404 and r.json()["message"] == "watchlist non trovata"
    assert (await c.delete(f"/api/agents/{b}/watchlists/{book['tech']}")).status_code == 404
    r = await c.post(f"/api/agents/{b}/alerts/{book['alert']}/cancel")
    assert r.status_code == 404 and r.json()["message"] == "alert non trovato"
    r = await c.delete(f"/api/agents/{book['aid']}/watchlists/{book['tech']}/items/NVDA")
    assert r.status_code == 404 and r.json()["message"] == "simbolo non presente nella watchlist"


async def test_remove_untracks_only_unreferenced_with_audit_and_notification(
    admin, book, redis_url
):
    c, a = admin.client, book["aid"]
    r = await c.delete(f"/api/agents/{a}/watchlists/{book['tech']}/items/MSFT")
    assert r.status_code == 200 and r.json()["symbols"] == ["AAPL"]
    r = await c.delete(f"/api/agents/{a}/watchlists/{book['tech']}/items/AAPL")
    assert r.status_code == 200
    tracked = await _tracked(redis_url, "watchlists")
    assert "MSFT" not in tracked and "AAPL" in tracked  # AAPL still in "other"

    r = await c.delete(f"/api/agents/{a}/watchlists/{book['other']}")
    assert r.status_code == 200
    assert "AAPL" not in await _tracked(redis_url, "watchlists")
    assert "AAPL" in await _tracked(redis_url, "alerts")  # the alert still tracks it

    r = await c.post(f"/api/agents/{a}/alerts/{book['alert']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert "AAPL" not in await _tracked(redis_url, "alerts")
    assert (await c.get(f"/api/agents/{a}/alerts")).json() == []

    notes = await _notes(admin.sm, a)
    assert [n["action"] for n in notes] == [
        "remove_symbol", "remove_symbol", "delete_watchlist", "cancel_alert"]
    assert notes[0] == {"action": "remove_symbol", "watchlist": "tech",
                        "watchlist_id": str(book["tech"]), "symbol": "MSFT"}
    assert await _audit(admin.sm) == [
        "admin:watchlist.remove_symbol", "admin:watchlist.remove_symbol",
        "admin:watchlist.delete", "admin:alert.cancel"]


async def test_mutations_require_csrf(admin, book):
    transport = httpx.ASGITransport(app=admin.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies=admin.client.cookies) as no_csrf:
        r = await no_csrf.delete(f"/api/agents/{book['aid']}/watchlists/{book['tech']}")
    assert r.status_code == 403
