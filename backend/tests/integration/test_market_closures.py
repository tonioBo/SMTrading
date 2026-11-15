"""Manual market closures (instruments.closures): admin API /api/market-closures (audited,
applied at once in the API process), the 60 s refresher of the worker, BrokerSync
waiting on a manually closed day (Review Focus 2) and London in get_market_status."""

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import ClassVar

import exchange_calendars as xcals
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers import registry
from smtrading.brokers.base import BrokerAdapter, BrokerOrder
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.models import OrderType
from smtrading.instruments.market_hours import Closure, market_status, set_closures
from smtrading.instruments.models import AssetClass

D = Decimal
PASSWORD = "correct-horse-battery"
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    set_closures([])
    yield
    set_closures([])


def _next_nyse_session() -> date:
    """A NYSE trading day at least two days ahead (never «today», never a holiday)."""
    cal = xcals.get_calendar("XNYS")
    return cal.date_to_session(date.today() + timedelta(days=2), direction="next").date()


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
            ns.client, ns.sm, ns.email = client, sm, email
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _audits(sm, method):
    from smtrading.agents.models import McpAuditLog

    async with sm() as s:
        return list(await s.scalars(select(McpAuditLog.params)
                                    .where(McpAuditLog.method == method)
                                    .order_by(McpAuditLog.id)))


async def test_closure_crud_audited_and_refreshed(admin):
    c, day = admin.client, _next_nyse_session()
    at = datetime.combine(day, time(15, 30), tzinfo=UTC)  # inside NYSE hours
    assert market_status("NYSE", at).is_open
    r = await c.post("/api/market-closures", json={
        "market": "NYSE", "day": day.isoformat(), "kind": "closed", "note": "Lutto nazionale"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["market"] == "NYSE" and created["kind"] == "closed"
    assert created["close_time"] is None and created["created_by"] == f"admin:{admin.email}"
    assert not market_status("NYSE", at).is_open  # applied at once, no refresher wait

    r = await c.post("/api/market-closures", json={
        "market": "LSE", "day": day.isoformat(), "kind": "early_close",
        "close_time": "12:30", "note": "Mezza giornata"})
    assert r.status_code == 200, r.text
    assert r.json()["close_time"] == "12:30"
    listed = (await c.get("/api/market-closures")).json()
    assert [(x["market"], x["kind"]) for x in listed] == [("LSE", "early_close"),
                                                         ("NYSE", "closed")]
    assert (await c.get("/api/market-closures",
                        params={"from": (day + timedelta(days=1)).isoformat()})).json() == []

    r = await c.delete(f"/api/market-closures/{created['id']}")
    assert r.status_code == 200, r.text
    assert market_status("NYSE", at).is_open
    assert (await c.delete(f"/api/market-closures/{created['id']}")).status_code == 404
    assert [a["closure_id"] for a in await _audits(admin.sm, "admin:market_closure.add")] == [
        created["id"], listed[0]["id"]]
    assert [a["closure_id"] for a in await _audits(admin.sm, "admin:market_closure.delete")
            ] == [created["id"]]


@pytest.mark.parametrize("body,field", [
    ({"market": "CRYPTO"}, "market"),
    ({"market": "XXX"}, "market"),
    ({"day": "past"}, "day"),
    ({"kind": "early_close"}, "close_time"),
    ({"kind": "half"}, "kind"),
    ({"note": "x" * 201}, "note"),
])
async def test_closure_validation(admin, body, field):
    day = _next_nyse_session()
    payload = {"market": "NYSE", "day": day.isoformat(), "kind": "closed", "note": "x"}
    payload.update(body)
    if payload["day"] == "past":
        payload["day"] = (date.today() - timedelta(days=1)).isoformat()
    r = await admin.client.post("/api/market-closures", json=payload)
    assert r.status_code == 400 and r.json()["details"]["field"] == field, r.text


async def test_closure_duplicate(admin):
    payload = {"market": "NYSE", "day": _next_nyse_session().isoformat(), "kind": "closed",
               "note": ""}
    assert (await admin.client.post("/api/market-closures", json=payload)).status_code == 200
    r = await admin.client.post("/api/market-closures", json=payload)
    assert r.status_code == 400 and r.json()["details"]["field"] == "day"


async def test_refresher_picks_up_new_rows(fresh_db):
    """Review Focus 2: a row written by another process (the API) reaches the worker's
    registry on its next tick, without a restart."""
    from smtrading.instruments.closures import ClosureRefresher, MarketClosure

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    day = _next_nyse_session()
    at = datetime.combine(day, time(15, 30), tzinfo=UTC)
    try:
        refresher = ClosureRefresher(sm, interval_s=0.01)
        assert await refresher.tick() == 0
        assert market_status("NYSE", at).is_open
        async with sm() as s:
            s.add(MarketClosure(market="NYSE", day=day, kind="closed", note="",
                                created_by="admin:x"))
            s.add(MarketClosure(market="NYSE", day=date.today() - timedelta(days=30),
                                kind="closed", note="", created_by="admin:x"))  # too old
            await s.commit()
        assert await refresher.tick() == 1
        assert not market_status("NYSE", at).is_open
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ BrokerSync

PLACED: list[str] = []


@registry.register
class _ClosureBrokerAdapter(BrokerAdapter):
    kind: ClassVar[str] = "fake_closure_test"
    supported_types: ClassVar[frozenset] = frozenset(OrderType)
    supported_asset_classes: ClassVar[frozenset] = frozenset(AssetClass)

    async def place_order(self, order, instrument) -> BrokerOrder:
        PLACED.append(str(order.id))
        return BrokerOrder(f"c{len(PLACED)}", "accepted", D(0), None, None)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return BrokerOrder(broker_order_id, "accepted", D(0), None, None)

    async def cancel_order(self, broker_order_id: str) -> None:
        return None

    async def get_positions(self) -> dict[str, Decimal]:
        return {}

    async def find_order(self, order, instrument, since, exclude=frozenset()):
        return None


async def test_order_waits_on_manual_closure(fresh_db, redis_url, monkeypatch):
    """Review Focus 2: a market order of a linked portfolio stays `to_send` while
    BrokerSync ticks on a manually closed day; it is sent once the closure is gone."""
    from smtrading.brokers.service import create_link, link_portfolio
    from smtrading.brokers.sync import BrokerSync
    from smtrading.engine.models import BrokerState, Order, OrderSide
    from smtrading.engine.quotesource import FakeQuoteSource
    from smtrading.engine.service import PlaceOrderRequest, place_order
    from smtrading.events import dispatch_post_commit
    from smtrading.portfolios.service import create_portfolio
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    get_settings.cache_clear()
    PLACED.clear()
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(redis_url)
    try:
        async with sm() as s:
            await run_seed(s)
            p = await create_portfolio(s, name="Chiusure", initial_cash=D(100000),
                                       leverage=D(1))
            link = await create_link(s, "L-closure", "fake_closure_test", {})
            await link_portfolio(s, p.id, link.id)
            await s.commit()
        async with sm() as s:
            order = await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=D(1)), now=OPEN)
            await s.commit()
            await dispatch_post_commit(s, redis)
        set_closures([Closure("NASDAQ", OPEN.date(), "closed", None)])
        sync = BrokerSync(sm, redis, qs, None, clock=lambda: OPEN)
        await sync.tick()
        async with sm() as s:
            o = await s.get(Order, order.id)
            assert o.broker_status == BrokerState.TO_SEND and o.broker_order_id is None
        assert PLACED == []
        set_closures([])
        await sync.tick()
        assert PLACED == [str(order.id)]
    finally:
        await redis.aclose()
        await engine.dispose()


async def test_mcp_market_status_includes_lse(mcp_server, mcp_call):
    err, out = await mcp_call(mcp_server.url, mcp_server.token, "get_market_status", {})
    assert not err and "LSE" in {m["market"] for m in out}
    err, out = await mcp_call(mcp_server.url, mcp_server.token, "get_market_status",
                              {"market": "lse"})
    assert not err and out["market"] == "LSE"
