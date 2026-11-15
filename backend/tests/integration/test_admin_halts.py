"""Admin API «Blocco aperture» (/api/halts), the halt permission (PATCH can_halt) and the
CLI `agent halt-permission`."""

import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

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
            ns.client, ns.sm, ns.email, ns.redis_url = client, sm, email, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _audits(admin, method):
    from smtrading.agents.models import McpAuditLog

    async with admin.sm() as s:
        rows = list(await s.scalars(select(McpAuditLog.params)
                                    .where(McpAuditLog.method == method)
                                    .order_by(McpAuditLog.id)))
    # a portal request's row carries its activity log request_id (see test_user_activity)
    return [{k: v for k, v in p.items() if k != "request_id"} for p in rows]


async def _portfolio_id(admin, name="Demo"):
    from smtrading.portfolios.models import Portfolio

    async with admin.sm() as s:
        return await s.scalar(select(Portfolio.id).where(Portfolio.name == name))


async def test_set_list_clear(admin):
    c = admin.client
    assert (await c.get("/api/halts")).json() == {"active": [], "history": []}

    r = await c.post("/api/halts", json={"reason": "  Volatilità alta  "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    halt = body["halt"]
    assert halt["scope"] == "global" and halt["reason"] == "Volatilità alta"
    assert halt["set_by"] == f"admin:{admin.email}" and halt["set_by_name"] == admin.email
    assert halt["active"] is True and halt["set_at"]

    demo = await _portfolio_id(admin)
    r = await c.post("/api/halts", json={"scope": "portfolio", "portfolio_id": str(demo),
                                         "reason": "Solo Demo"})
    assert r.status_code == 200 and r.json()["halt"]["portfolio"] == "Demo"
    listed = (await c.get("/api/halts")).json()
    assert [h["scope"] for h in listed["active"]] == ["portfolio", "global"]

    r = await c.post(f"/api/halts/{halt['id']}/clear")
    assert r.status_code == 200 and r.json()["changed"] is True
    cleared = r.json()["halt"]
    assert cleared["active"] is False and cleared["cleared_by_name"] == admin.email
    r = await c.post(f"/api/halts/{halt['id']}/clear")  # already cleared
    assert r.status_code == 200 and r.json()["changed"] is False
    assert (await c.post("/api/halts/999999/clear")).status_code == 404

    listed = (await c.get("/api/halts")).json()
    assert [h["scope"] for h in listed["active"]] == ["portfolio"]
    assert [h["id"] for h in listed["history"]] == [halt["id"]]
    assert [p["halt_id"] for p in await _audits(admin, "admin:halt.set")] == [
        halt["id"], listed["active"][0]["id"]]
    assert await _audits(admin, "admin:halt.clear") == [
        {"halt_id": halt["id"], "by": admin.email}]


@pytest.mark.parametrize("body,field", [
    ({"reason": ""}, "reason"),
    ({"reason": "x" * 501}, "reason"),
    ({"scope": "all", "reason": "x"}, "scope"),
    ({"scope": "portfolio", "reason": "x"}, "portfolio_id"),
])
async def test_invalid_bodies(admin, body, field):
    r = await admin.client.post("/api/halts", json=body)
    assert r.status_code == 400 and r.json()["details"]["field"] == field


async def test_routes_require_login(admin):
    async with httpx.AsyncClient(transport=admin.client._transport,
                                 base_url="http://test") as anon:
        for method, path in (("GET", "/api/halts"), ("POST", "/api/halts"),
                             ("POST", "/api/halts/1/clear")):
            assert (await anon.request(method, path, json={})).status_code == 401


@pytest.mark.parametrize("path,body", [
    ("/api/halts", {"reason": "x"}),
    ("/api/halts/1/clear", None),
])
async def test_mutating_routes_require_csrf(admin, path, body):
    async with httpx.AsyncClient(transport=admin.client._transport, base_url="http://test",
                                 cookies=admin.client.cookies) as no_csrf:
        r = await no_csrf.post(path, json=body)
    assert r.status_code == 403, r.text
    assert (await admin.client.get("/api/halts")).json()["active"] == []


async def test_patch_agent_can_halt_audited(admin):
    c = admin.client
    agent = (await c.post("/api/agents", json={"name": "CEO SMTrading"})).json()["agent"]
    assert agent["can_halt"] is False
    r = await c.patch(f"/api/agents/{agent['id']}", json={"can_halt": True})
    assert r.status_code == 200 and r.json()["can_halt"] is True
    assert r.json()["can_notify"] is False
    assert (await c.patch(f"/api/agents/{agent['id']}", json={"can_halt": None})).status_code \
        == 400
    assert await _audits(admin, "admin:agent.halt") == [
        {"agent_id": agent["id"], "agent": "CEO SMTrading", "can_halt": True, "by": admin.email}]


async def test_cli_agent_halt_permission(admin, monkeypatch):
    from redis.asyncio import Redis

    from smtrading import cli
    from smtrading.agents import service as agents

    async with admin.sm() as s:
        await agents.create_agent(s, "CEO SMTrading")
        await s.commit()

    async def run(*argv):
        redis = Redis.from_url(admin.redis_url)
        try:
            return await cli.run_command(cli.build_parser().parse_args(list(argv)), admin.sm,
                                         None, redis)
        finally:
            await redis.aclose()

    code, out = await run("agent", "halt-permission", "CEO SMTrading", "on")
    assert code == 0 and json.loads(out)["can_halt"] is True
    code, out = await run("agent", "halt-permission", "CEO SMTrading", "off")
    assert json.loads(out)["can_halt"] is False
    assert [p["can_halt"] for p in await _audits(admin, "admin:agent.halt")] == [True, False]
    code, _ = await run("agent", "halt-permission", "Nessuno", "on")
    assert code == 1
