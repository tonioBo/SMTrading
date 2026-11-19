"""Admin API «Riconciliazione» (plan 21): the list with not-linked portfolios, one
portfolio's latest check and history, «Riconcilia ora» (admins only, read-only)."""

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
import test_reconciliation as recon  # the fake broker account (registers fake_recon)
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.base import BrokerOpenOrder
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.portfolios.service import create_portfolio

D = Decimal
PASSWORD = "correct-horse-battery"


async def _login(app, email: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]
    return client


@pytest.fixture
async def api(fresh_db, redis_url, monkeypatch):
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
    recon.ACCOUNT = recon.Account()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    admin, viewer = (f"{r}-{uuid.uuid4().hex[:8]}@example.com" for r in ("admin", "viewer"))
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=admin, password_hash=await hash_password(PASSWORD)))
        s.add(AdminUser(email=viewer, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        p = await create_portfolio(s, name="Alfa collegato", initial_cash=D(10000))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_recon", {})
        await link_portfolio(s, p.id, link.id)
        discard_post_commit(s)
        await s.commit()
        pid = p.id

    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        clients = [await _login(app, admin), await _login(app, viewer)]

        class NS:
            pass

        ns = NS()
        ns.admin, ns.viewer, ns.pid, ns.link_id, ns.sm = *clients, pid, link.id, sm
        yield ns
        for c in clients:
            await c.aclose()
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def test_list_shows_linked_and_not_linked_portfolios(api):
    r = await api.admin.get("/api/reconciliation")
    assert r.status_code == 200
    rows = r.json()["portfolios"]
    assert rows[0]["portfolio_name"] == "Alfa collegato" and rows[0]["linked"] is True
    assert rows[0]["latest"] is None and rows[0]["link"]["live"] is False
    others = [p for p in rows if not p["linked"]]
    assert others and all(p["link"] is None and p["latest"] is None for p in others)


async def test_run_now_then_history(api):
    recon.ACCOUNT.positions = {"MSFT": D(3)}
    recon.ACCOUNT.orders = [BrokerOpenOrder("x1", "MSFT", "buy", D(1), D(10), None)]
    r = await api.admin.post("/api/reconciliation/run", json={"portfolio_id": str(api.pid)})
    assert r.status_code == 200, r.text
    latest = next(p for p in r.json()["portfolios"] if p["portfolio_id"] == str(api.pid))
    assert latest["latest"]["status"] == "mismatch"
    assert latest["latest"]["trigger"] == "manual"
    assert latest["latest"]["counts"]["mismatch"] == 2
    keys = {i["key"] for i in latest["latest"]["items"]}
    assert {"position:MSFT", "order:x1", "cash:EUR"} <= keys
    assert "place_order" not in recon.ACCOUNT.calls

    recon.ACCOUNT.fail = "fake: HTTP 503"
    r = await api.admin.post("/api/reconciliation/run", json={})
    assert r.status_code == 200
    r = await api.admin.get(f"/api/reconciliation/{api.pid}", params={"limit": "5"})
    body = r.json()
    assert body["latest"]["status"] == "unverifiable" and "503" in body["latest"]["error"]
    assert body["last_verified_at"] == latest["latest"]["ts"]
    assert [h["status"] for h in body["history"]] == ["unverifiable", "mismatch"]
    assert "items" not in body["history"][0]


async def test_run_now_errors_and_viewer(api):
    r = await api.viewer.post("/api/reconciliation/run", json={})
    assert r.status_code == 403
    assert (await api.viewer.get("/api/reconciliation")).status_code == 200
    rows = (await api.admin.get("/api/reconciliation")).json()["portfolios"]
    unlinked = next(p for p in rows if not p["linked"])
    r = await api.admin.post("/api/reconciliation/run",
                             json={"portfolio_id": unlinked["portfolio_id"]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER"
    r = await api.admin.get(f"/api/reconciliation/{uuid.uuid4()}")
    assert r.status_code == 404
