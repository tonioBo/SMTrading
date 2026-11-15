"""Admin API: manual orders (plan 9, Task 5) — preview, placement with every risk check
(kill switch, «Blocco aperture», limits), stored rejections, the audit, and on a real-money
portfolio the TOTP (Review Focus 5) and the live path (dry run through BrokerSync).

Never reaches the network: the live broker is the in-memory FakeLiveBroker (BrokerSync's
inner transport) and a dry-run link never sends. Canary-free: live links carry no keys.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pyotp
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.models import McpAuditLog
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.engine.models import BrokerState, Order, OrderStatus

D = Decimal
PASSWORD = "correct-horse-battery"


class Fake(FakeLiveBroker):
    """FakeLiveBroker recording every request that reached it."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        return super()._handle(request)


class Codes:
    """Valid TOTP codes; `fresh()` clears the replay marks first, `now()` does not."""

    def __init__(self, secret: str, admin_id, redis_url: str) -> None:
        self.totp = pyotp.TOTP(secret)
        self.admin_id = admin_id
        self.redis_url = redis_url

    async def fresh(self) -> str:
        r = Redis.from_url(self.redis_url)
        try:
            keys = [k async for k in r.scan_iter(f"adm:totp_used:{self.admin_id}:*")]
            if keys:
                await r.delete(*keys)
        finally:
            await r.aclose()
        return self.totp.now()


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    """`client`: an admin with TOTP on; `plain`: an admin without TOTP."""
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
    secret = pyotp.random_base32()
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    plain_email = f"plain-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        user = AdminUser(
            email=email, password_hash=await hash_password(PASSWORD), totp_enabled=True,
            totp_secret_encrypted=encrypt_json({"secret": secret}, get_settings().master_key))
        s.add(user)
        s.add(AdminUser(email=plain_email, password_hash=await hash_password(PASSWORD)))
        await s.commit()
    codes = Codes(secret, user.id, redis_url)

    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_fx("USD", "EUR", D("0.8"))
    qs.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    qs.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    fake = Fake()
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    transport = httpx.ASGITransport(app=app)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            httpx.AsyncClient(transport=transport, base_url="http://test") as plain,
        ):
            r = await client.post("/api/auth/login", json={
                "email": email, "password": PASSWORD, "totp": await codes.fresh()})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]
            r = await plain.post("/api/auth/login",
                                 json={"email": plain_email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            plain.headers["X-CSRF-Token"] = plain.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.plain, ns.sm, ns.qs, ns.app = client, plain, sm, qs, app
            ns.codes, ns.fake, ns.email, ns.redis_url = codes, fake, email, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _profile(c, **kw) -> str:
    body = {"name": f"R-{uuid.uuid4().hex[:6]}", "max_order_notional_eur": "100000",
            "max_daily_loss_pct": "50", "short_allowed": False, **kw}
    r = await c.post("/api/risk-profiles", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _portfolio(c, cash="10000", **profile) -> str:
    r = await c.post("/api/portfolios", json={
        "name": f"P-{uuid.uuid4().hex[:6]}", "initial_cash": cash,
        "risk_profile_id": await _profile(c, **profile)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _live(admin, *, state="dry_run", cash="10000") -> str:
    """A EUR portfolio linked to a keyless real-money link (nothing real, canary-free)."""
    pid = await _portfolio(admin.client)
    async with admin.sm() as s:
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind="alpaca_live",
                          live_state=state,
                          last_account_snapshot={"currency": "USD", "cash_available": cash},
                          last_snapshot_at=datetime.now(UTC))
        s.add(link)
        await s.flush()
        from smtrading.portfolios.models import Portfolio

        (await s.get(Portfolio, uuid.UUID(pid))).broker_link_id = link.id
        await s.commit()
    return pid


def _btc(**kw) -> dict:
    return {"symbol": "BTC-USD", "side": "buy", "type": "market", "qty": "0.01", **kw}


def _aapl(**kw) -> dict:
    return {"symbol": "AAPL", "side": "buy", "type": "market", "qty": "1", **kw}


async def _orders(sm, pid) -> list[Order]:
    async with sm() as s:
        return list(await s.scalars(
            select(Order).where(Order.portfolio_id == uuid.UUID(pid))
            .order_by(Order.created_at, Order.id)))


async def _order_count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(Order))


async def _audits(sm, method: str) -> list[McpAuditLog]:
    async with sm() as s:
        return list(await s.scalars(
            select(McpAuditLog).where(McpAuditLog.method == method).order_by(McpAuditLog.id)))


# -- preview ------------------------------------------------------------------------------

async def test_admin_preview_runs_risk_checks(admin):
    c = admin.client
    pid = await _portfolio(c, max_order_notional_eur="100")
    r = await c.post(f"/api/portfolios/{pid}/orders/preview", json=_btc())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is False and out["code"] == "RISK_LIMIT_EXCEEDED"
    assert out["real"] is False and out["currency"] == "EUR"
    assert await _orders(admin.sm, pid) == []

    small = await c.post(f"/api/portfolios/{pid}/orders/preview", json=_btc(qty="0.001"))
    got = small.json()
    assert got["ok"] is True and got["code"] is None, got
    assert D(got["estimated_price"]) == D("50010.00")
    assert D(got["estimated_notional_eur"]) > 0 and got["buying_power_after_eur"] is not None
    assert set(got) >= {"estimated_fee", "estimated_fx_fee_eur"}
    assert await _orders(admin.sm, pid) == []


async def test_admin_real_preview_needs_no_totp(admin):
    pid = await _live(admin)
    for client in (admin.client, admin.plain):  # even the admin without TOTP
        r = await client.post(f"/api/portfolios/{pid}/orders/preview", json=_aapl())
        assert r.status_code == 200, r.text
        assert r.json()["real"] is True and r.json()["ok"] is True, r.json()
    assert await _orders(admin.sm, pid) == []


# -- placement (simulated) --------------------------------------------------------------------

async def test_admin_place_simulated_order_source_admin(admin):
    c = admin.client
    pid = await _portfolio(c)
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(),
                     headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "filled" and out["symbol"] == "BTC-USD"
    listed = (await c.get(f"/api/portfolios/{pid}/orders")).json()
    assert listed == [out]  # the same DTO as the order list
    (order,) = await _orders(admin.sm, pid)
    assert order.source == "admin" and order.agent_id is None
    # the same Idempotency-Key gives the same response, no second order
    again = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(),
                         headers={"Idempotency-Key": "k-1"})
    assert again.status_code == 200 and again.json() == out
    assert len(await _orders(admin.sm, pid)) == 1

    (audit,) = await _audits(admin.sm, "admin:order.place")
    assert audit.agent_id is None and audit.outcome == "ok"
    p = audit.params
    assert p["portfolio_id"] == pid and p["symbol"] == "BTC-USD" and p["side"] == "buy"
    assert p["type"] == "market" and D(p["qty"]) == D("0.01") and p["notional"] is None
    assert p["real"] is False and p["order_id"] == out["id"] and p["status"] == "filled"
    assert p["by"] == admin.email and "totp" not in p


async def test_admin_order_kill_switch_blocks_buy_allows_close(admin):
    c = admin.client
    pid = await _portfolio(c)
    assert (await c.post(f"/api/portfolios/{pid}/orders", json=_btc())).status_code == 200
    r = await c.post("/api/kill-switch", json={"on": True})
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc())
    assert r.status_code == 400 and r.json()["code"] == "KILL_SWITCH_ACTIVE", r.text
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(side="sell"))
    assert r.status_code == 200 and r.json()["status"] == "filled", r.text
    statuses = [(o.side.value, o.status.value) for o in await _orders(admin.sm, pid)]
    assert statuses == [("buy", "filled"), ("buy", "rejected"), ("sell", "filled")]


async def test_admin_order_halt_blocks_opening_allows_close(admin):
    c = admin.client
    pid = await _portfolio(c)
    assert (await c.post(f"/api/portfolios/{pid}/orders", json=_btc())).status_code == 200
    r = await c.post("/api/halts", json={"scope": "portfolio", "portfolio_id": pid,
                                         "reason": "Volatilità"})
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc())
    assert r.status_code == 400 and r.json()["code"] == "TRADING_HALTED", r.text
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(side="sell"))
    assert r.status_code == 200 and r.json()["status"] == "filled", r.text


async def test_admin_rejection_is_stored(admin):
    c = admin.client
    pid = await _portfolio(c, cash="100")
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(),
                     headers={"Idempotency-Key": "k-rej"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "INSUFFICIENT_BUYING_POWER"
    (order,) = await _orders(admin.sm, pid)
    assert order.status == OrderStatus.rejected and order.source == "admin"
    assert order.reject_code == "INSUFFICIENT_BUYING_POWER"
    assert r.json()["details"]["order_id"] == str(order.id)
    (audit,) = await _audits(admin.sm, "admin:order.place")
    assert audit.outcome == "INSUFFICIENT_BUYING_POWER"
    assert audit.params["status"] == "rejected" and audit.params["order_id"] == str(order.id)
    # a malformed request stores nothing
    r = await c.post(f"/api/portfolios/{pid}/orders", json=_btc(qty=None))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER"
    assert len(await _orders(admin.sm, pid)) == 1


# -- placement (real money, Review Focus 5) -------------------------------------------------

async def test_admin_real_order_requires_totp(admin):
    pid = await _live(admin)
    before = await _order_count(admin.sm)
    for totp in (None, "000000"):
        r = await admin.client.post(f"/api/portfolios/{pid}/orders",
                                    json=_aapl(totp=totp))
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "FORBIDDEN" and r.json()["details"]["field"] == "totp"
    assert await _order_count(admin.sm) == before
    assert await _audits(admin.sm, "admin:order.place") == []


async def test_admin_real_order_without_totp_enabled_forbidden(admin):
    pid = await _live(admin)
    before = await _order_count(admin.sm)
    r = await admin.plain.post(f"/api/portfolios/{pid}/orders",
                               json=_aapl(totp=admin.codes.totp.now()))
    assert r.status_code == 403, r.text
    assert r.json()["message"] == "Attiva prima l'autenticazione a due fattori"
    assert r.json()["details"]["field"] == "totp"
    assert await _order_count(admin.sm) == before


async def test_admin_real_order_totp_replay_forbidden(admin):
    pid = await _live(admin)
    code = await admin.codes.fresh()
    r = await admin.client.post(f"/api/portfolios/{pid}/orders", json=_aapl(totp=code))
    assert r.status_code == 200, r.text
    count = await _order_count(admin.sm)
    r = await admin.client.post(f"/api/portfolios/{pid}/orders", json=_aapl(totp=code))
    assert r.status_code == 403 and r.json()["message"] == "codice TOTP già usato", r.text
    assert await _order_count(admin.sm) == count


async def test_admin_real_order_with_totp_goes_through_live_checks(admin, monkeypatch):
    from smtrading.brokers import sync
    from smtrading.brokers.sync import BrokerSync
    from smtrading.instruments.market_hours import MarketStatus

    pid = await _live(admin)
    r = await admin.client.post(f"/api/portfolios/{pid}/orders",
                                json=_aapl(totp=await admin.codes.fresh()))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["broker_link_id"] is not None and out["broker_status"] == BrokerState.TO_SEND
    (audit,) = await _audits(admin.sm, "admin:order.place")
    assert audit.params["real"] is True and audit.outcome == "ok"

    monkeypatch.setattr(sync, "_default_inner", admin.fake.transport)
    monkeypatch.setattr(sync, "market_status",
                        lambda market, at: MarketStatus(market, True, None, None))
    redis = Redis.from_url(admin.redis_url)
    bs = BrokerSync(admin.sm, redis, admin.qs, None)
    try:
        await bs.tick()
    finally:
        await bs.aclose()
        await redis.aclose()
    (order,) = await _orders(admin.sm, pid)
    assert order.status == OrderStatus.rejected and order.reject_code == "LIVE_DRY_RUN"
    assert order.source == "admin" and order.agent_id is None
    assert admin.fake.requests == []  # a dry run never reaches the broker


# -- cancel -------------------------------------------------------------------------------------

async def test_admin_cancel_audited(admin):
    c = admin.client
    pid = await _portfolio(c)
    r = await c.post(f"/api/portfolios/{pid}/orders",
                     json=_btc(type="limit", limit_price="1000.00"))
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    oid = r.json()["id"]
    r = await c.post(f"/api/orders/{oid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "canceled", r.text
    (audit,) = await _audits(admin.sm, "admin:order.cancel")
    assert audit.agent_id is None and audit.outcome == "ok"
    assert audit.params["order_id"] == oid and audit.params["portfolio_id"] == pid
    assert audit.params["by"] == admin.email and audit.params["status"] == "canceled"
