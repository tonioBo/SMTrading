"""Admin API of real money: the link wizard (keys, read-only test, dry run, TOTP arm,
portfolio link, one-click disarm), the live audit with its CSV, the status banner, the
paper routes refusing (or re-routing) live links, the portfolio DTOs (`mode`,
`live_state`), the risk-profile fuse and the CLI disarm.

Never reaches the network: the live broker is the in-memory FakeLiveBroker behind the
live gate (app.state.live_http_factory), plus the session socket guard. Canary keys only.
"""

import asyncio
import base64
import csv
import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pyotp
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.models import LiveAudit
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderStatus, OrderType
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox

D = Decimal
PASSWORD = "correct-horse-battery"
KEY_ID = "CANARYKEYID0001"
SECRET = "CANARYSECRET-do-not-use"
BASIC = base64.b64encode(f"{KEY_ID}:{SECRET}".encode()).decode()
CANARIES = (KEY_ID, SECRET, BASIC)
T212 = {"api_key": KEY_ID, "api_secret": SECRET}


class Fake(FakeLiveBroker):
    """FakeLiveBroker that can answer 401 echoing the key (and the Basic credential)."""

    def __init__(self) -> None:
        super().__init__()
        self.unauthorized = False
        self.writes: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in ("GET", "HEAD"):
            self.writes.append(f"{request.method} {request.url.path}")
        if self.unauthorized:
            auth = request.headers.get("Authorization", "")
            return httpx.Response(401, json={"message": f"bad key {KEY_ID} {SECRET} {auth}"})
        return super()._handle(request)


class Codes:
    """Valid TOTP codes for the admin: the replay marks are cleared before each one
    (the replay guard itself is tested with the service)."""

    def __init__(self, secret: str, admin_id, redis_url: str) -> None:
        self.totp = pyotp.TOTP(secret)
        self.admin_id = admin_id
        self.redis_url = redis_url

    async def code(self) -> str:
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
    qs.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    fake = Fake()
    app.state.live_http_factory = lambda link_id: make_live_client(
        sm, link_id, inner=fake.transport())
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
                "email": email, "password": PASSWORD, "totp": await codes.code()})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]
            client.headers["User-Agent"] = "pytest-live"
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


async def _telegram(sm) -> None:
    async with sm() as s:
        await save_config(s, TelegramConfig(bot_token="123456789:AAH" + "x" * 32,
                                            chat_id="-100123", enabled=True))
        s.add(TelegramOutbox(source="system", category="test", level="info", text="prova",
                             status="sent", sent_at=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()


async def _new_link(c, name="Reale T212") -> dict:
    r = await c.post("/api/live-links", json={"name": name, "kind": "t212_live", **T212})
    assert r.status_code == 200, r.text
    return r.json()


async def _profile(c, **kw) -> str:
    body = {"name": f"Live-{uuid.uuid4().hex[:6]}", "max_order_notional_eur": "100",
            "max_daily_loss_pct": "5", "short_allowed": False, **kw}
    r = await c.post("/api/risk-profiles", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _portfolio(c, cash="500") -> dict:
    r = await c.post("/api/portfolios", json={
        "name": f"P-{uuid.uuid4().hex[:6]}", "initial_cash": cash,
        "risk_profile_id": await _profile(c)})
    assert r.status_code == 200, r.text
    return r.json()


async def _arm(admin, link: dict) -> httpx.Response:
    return await admin.client.post(f"/api/live-links/{link['id']}/arm", json={
        "totp": await admin.codes.code(), "confirm_name": link["name"],
        "accept_risks": True, "dedicated_account": True, "limits_set": True})


async def _wizard(admin) -> tuple[dict, dict]:
    """A t212_live link tested, armed and linked to an EUR portfolio."""
    c = admin.client
    await _telegram(admin.sm)
    link = await _new_link(c)
    assert (await c.post(f"/api/live-links/{link['id']}/test")).status_code == 200
    r = await _arm(admin, link)
    assert r.status_code == 200, r.text
    pf = await _portfolio(c)
    r = await c.put(f"/api/portfolios/{pf['id']}/live-link",
                    json={"link_id": link["id"], "totp": await admin.codes.code()})
    assert r.status_code == 200, r.text
    return link, pf


async def _audit_actions(sm, link_id) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(LiveAudit.action).where(
            LiveAudit.link_id == uuid.UUID(link_id)).order_by(LiveAudit.id)))


# -- the wizard ------------------------------------------------------------------------

async def test_wizard_flow_api(admin):
    c = admin.client
    await _telegram(admin.sm)
    link = await _new_link(c)
    assert link["live_state"] == "configured" and link["kind"] == "t212_live"
    assert link["key_masked"] == "••••0001" and link["snapshot"] is None
    assert link["portfolio"] is None and link["health_failures"] == 0

    r = await c.post(f"/api/live-links/{link['id']}/test")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["empty"] is True and result["currency"] == "EUR"
    assert Decimal(result["cash_available"]) == Decimal(1000)

    r = await c.post(f"/api/live-links/{link['id']}/dry-run", json={"on": True})
    assert r.status_code == 200 and r.json()["live_state"] == "dry_run", r.text

    # arming with a wrong TOTP is refused, nothing changes
    r = await c.post(f"/api/live-links/{link['id']}/arm", json={
        "totp": "000000", "confirm_name": link["name"], "accept_risks": True,
        "dedicated_account": True, "limits_set": True})
    assert r.status_code == 403 and r.json()["details"]["field"] == "totp"
    r = await _arm(admin, link)
    assert r.status_code == 200, r.text
    armed = r.json()
    assert armed["live_state"] == "armed" and armed["armed_by"] == admin.email
    assert armed["snapshot"]["currency"] == "EUR" and armed["account_masked"]

    pf = await _portfolio(c)
    r = await c.put(f"/api/portfolios/{pf['id']}/live-link",
                    json={"link_id": link["id"], "totp": "000000"})
    assert r.status_code == 403
    r = await c.put(f"/api/portfolios/{pf['id']}/live-link",
                    json={"link_id": link["id"], "totp": await admin.codes.code()})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "reale" and r.json()["live_state"] == "armed"

    (listed,) = (await c.get("/api/live-links")).json()
    assert listed["portfolio"] == {"id": pf["id"], "name": pf["name"]}
    status = (await c.get("/api/live/status")).json()
    assert status["armed_count"] == 1 and status["dry_run_count"] == 0
    assert status["portfolios"] == [{"id": pf["id"], "name": pf["name"],
                                     "live_state": "armed"}]
    assert status["telegram_ok"] is True

    r = await c.post(f"/api/live-links/{link['id']}/disarm", json={})
    assert r.status_code == 200, r.text
    assert r.json()["live_state"] == "configured"
    assert r.json()["disarmed_reason"] == "disattivato dall'admin"
    assert (await c.get("/api/live/status")).json()["armed_count"] == 0
    assert await _audit_actions(admin.sm, link["id"]) == [
        "keys_saved", "test_ok", "dry_run_on", "armed", "portfolio_linked", "disarmed"]
    async with admin.sm() as s:
        row = await s.scalar(select(LiveAudit).where(LiveAudit.action == "armed"))
    assert row.actor == admin.email and row.user_agent == "pytest-live" and row.ip
    assert admin.fake.writes == []  # never a write at the broker


async def test_replace_keys_back_to_configured(admin):
    c = admin.client
    link, _ = await _wizard(admin)
    r = await c.put(f"/api/live-links/{link['id']}/keys",
                    json={"api_key": "CANARYKEYID0002", "api_secret": SECRET})
    assert r.status_code == 200, r.text
    assert r.json()["live_state"] == "configured" and r.json()["key_masked"] == "••••0002"
    assert r.json()["last_test_at"] is None


async def test_live_kinds_refused_on_paper_routes(admin):
    c = admin.client
    r = await c.post("/api/broker-links", json={
        "name": "X", "kind": "alpaca_live",
        "config": {"key_id": KEY_ID, "secret_key": SECRET}})
    assert r.status_code == 400 and r.json()["message"] == "usa la procedura «denaro reale»"
    link = await _new_link(c)
    r = await c.patch(f"/api/broker-links/{link['id']}", json={"name": "Y"})
    assert r.status_code == 400 and r.json()["message"] == "usa la procedura «denaro reale»"
    r = await c.patch(f"/api/broker-links/{link['id']}",
                      json={"config": {"api_key": KEY_ID, "api_secret": "other-secret-1"}})
    assert r.status_code == 400
    pf = await _portfolio(c)
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code in (400, 403)
    assert r.json()["message"] == "usa la procedura «denaro reale»"

    # the generic list shows the live link masked, with its live_state
    (row,) = [x for x in (await c.get("/api/broker-links")).json() if x["id"] == link["id"]]
    assert row["live_state"] == "configured"
    assert row["config_masked"] == {"api_key": "••••0001", "api_secret": "••••"}

    # test, positions and reconcile go through the live flow (read-only, gated)
    r = await c.post(f"/api/broker-links/{link['id']}/test")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert "test_ok" in await _audit_actions(admin.sm, link["id"])
    r = await c.get(f"/api/broker-links/{link['id']}/positions")
    assert r.status_code == 200 and r.json() == {"positions": []}, r.text
    r = await c.post(f"/api/broker-links/{link['id']}/reconcile")
    assert r.status_code == 200 and r.json() == {"reconciliations": []}, r.text
    assert admin.fake.writes == []


def test_link_secrets_cover_the_t212_live_basic_credential():
    from smtrading.admin.routes_brokers import link_secrets

    assert set(link_secrets("t212_live", T212)) == {KEY_ID, SECRET, BASIC}
    assert set(link_secrets("alpaca_live", {"key_id": KEY_ID, "secret_key": SECRET})) == {
        KEY_ID, SECRET}


async def test_live_routes_validation(admin):
    c = admin.client
    r = await c.post("/api/live-links", json={"name": "X", "kind": "alpaca_paper",
                                              "key_id": KEY_ID, "secret_key": SECRET})
    assert r.status_code == 400 and r.json()["details"]["field"] == "kind"
    r = await c.post("/api/live-links", json={"name": "X", "kind": "alpaca_live",
                                              "key_id": KEY_ID})
    assert r.status_code == 400 and r.json()["details"]["field"] == "secret_key"
    missing = uuid.uuid4()
    assert (await c.post(f"/api/live-links/{missing}/test")).status_code == 404
    assert (await c.post(f"/api/live-links/{missing}/disarm", json={})).status_code == 404
    # CSRF is required on every write
    r = await c.post("/api/live-links", json={"name": "X", "kind": "t212_live", **T212},
                     headers={"X-CSRF-Token": ""})
    assert r.status_code == 403


async def test_requires_session(admin):
    transport = httpx.ASGITransport(app=admin.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        for method, path in (("GET", "/api/live-links"), ("GET", "/api/live/status"),
                             ("GET", "/api/live-audit"), ("GET", "/api/live-audit.csv"),
                             ("POST", f"/api/live-links/{uuid.uuid4()}/disarm")):
            assert (await anon.request(method, path)).status_code == 401, path


async def test_arm_without_admin_totp_403_message(admin):
    await _telegram(admin.sm)
    link = await _new_link(admin.client)
    assert (await admin.client.post(f"/api/live-links/{link['id']}/test")).status_code == 200
    r = await admin.plain.post(f"/api/live-links/{link['id']}/arm", json={
        "totp": "123456", "confirm_name": link["name"], "accept_risks": True,
        "dedicated_account": True, "limits_set": True})
    assert r.status_code == 403
    assert r.json()["message"] == "Attiva prima l'autenticazione a due fattori"
    pf = await _portfolio(admin.client)
    for path, body in ((f"/api/portfolios/{pf['id']}/live-link",
                        {"link_id": link["id"], "totp": "123456"}),):
        r = await admin.plain.put(path, json=body)
        assert r.status_code == 403
        assert r.json()["message"] == "Attiva prima l'autenticazione a due fattori"
    for path in (f"/api/portfolios/{pf['id']}/live-resume",
                 f"/api/portfolios/{pf['id']}/live-flatten"):
        r = await admin.plain.post(path, json={"totp": "123456"})
        assert r.status_code == 403, path
        assert r.json()["message"] == "Attiva prima l'autenticazione a due fattori"


async def test_disarm_needs_no_totp(admin):
    link, pf = await _wizard(admin)
    r = await admin.plain.post(f"/api/live-links/{link['id']}/disarm",
                               json={"reason": "emergenza"})
    assert r.status_code == 200, r.text
    assert r.json()["live_state"] == "configured" and r.json()["disarmed_reason"] == "emergenza"
    got = (await admin.client.get(f"/api/portfolios/{pf['id']}")).json()
    assert got["mode"] == "paper" and got["live_state"] == "configured"


async def test_resume_and_flatten_routes(admin):
    c = admin.client
    link, pf = await _wizard(admin)
    async with admin.sm() as s:
        p = await s.get(Portfolio, uuid.UUID(pf["id"]))
        p.status, p.reduce_only_reason = PortfolioStatus.reduce_only, "live_fuse"
        await s.commit()
    r = await c.post(f"/api/portfolios/{pf['id']}/live-resume", json={"totp": "000000"})
    assert r.status_code == 403
    r = await c.post(f"/api/portfolios/{pf['id']}/live-resume",
                     json={"totp": await admin.codes.code()})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active" and r.json()["mode"] == "reale"
    r = await c.post(f"/api/portfolios/{pf['id']}/live-flatten",
                     json={"totp": await admin.codes.code()})
    assert r.status_code == 200, r.text
    assert r.json() == {"orders": []}  # no position
    assert await _audit_actions(admin.sm, link["id"]) == [
        "keys_saved", "test_ok", "armed", "portfolio_linked", "resumed", "flatten"]
    # unlink (no TOTP needed: it reduces what is at stake)
    r = await c.put(f"/api/portfolios/{pf['id']}/live-link", json={"link_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "simulazione" and r.json()["live_state"] is None


# -- live audit --------------------------------------------------------------------------

async def test_live_audit_filters_and_csv(admin):
    c = admin.client
    link, pf = await _wizard(admin)
    other = await _new_link(c, name="Altro")
    rows = (await c.get("/api/live-audit")).json()
    assert [r["action"] for r in rows][:2] == ["keys_saved", "portfolio_linked"]  # newest first
    assert all(rows[i]["id"] > rows[i + 1]["id"] for i in range(len(rows) - 1))
    first = rows[0]
    assert first["link_id"] == other["id"] and first["link_name"] == "Altro"
    assert first["actor"] == admin.email and first["details"]["key_masked"] == "••••0001"

    mine = (await c.get("/api/live-audit", params={"link_id": link["id"]})).json()
    assert [r["action"] for r in mine] == [
        "portfolio_linked", "armed", "test_ok", "keys_saved"]
    by_pf = (await c.get("/api/live-audit", params={"portfolio_id": pf["id"]})).json()
    assert [r["action"] for r in by_pf] == ["portfolio_linked"]
    assert by_pf[0]["portfolio_name"] == pf["name"]
    armed = (await c.get("/api/live-audit", params={"action": "armed"})).json()
    assert [r["action"] for r in armed] == ["armed"]
    page = (await c.get("/api/live-audit", params={"limit": 2})).json()
    assert len(page) == 2
    rest = (await c.get("/api/live-audit", params={"before_id": page[-1]["id"]})).json()
    assert [r["id"] for r in rest] == [r["id"] for r in rows[2:]]
    for bad in ({"limit": 0}, {"limit": 201}, {"action": "bogus"}):
        assert (await c.get("/api/live-audit", params=bad)).status_code == 400, bad

    r = await c.get("/api/live-audit.csv", params={"link_id": link["id"]})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    body = r.content.decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(body), delimiter=";"))
    assert table[0] == ["id", "ts", "collegamento", "portafoglio", "ordine", "attore",
                        "azione", "ip", "dettagli"]
    assert [row[6] for row in table[1:]] == [
        "portfolio_linked", "armed", "test_ok", "keys_saved"]
    assert table[1][2] == link["name"] and table[1][3] == pf["name"]
    assert json.loads(table[-1][8])["key_masked"] == "••••0001"


# -- secrets -----------------------------------------------------------------------------

async def _all_rows_text(sm) -> str:
    """Every row of every table as JSON text (bytea columns as hex)."""
    async with sm() as s:
        tables = list(await s.scalars(text(
            "select tablename from pg_tables where schemaname = 'public'")))
        out = []
        for t in tables:
            rows = await s.scalars(text(f'select row_to_json(x)::text from "{t}" x'))
            out.extend(rows)
    return "\n".join(out)


async def test_live_canary_keys_never_leak(admin, caplog):
    caplog.set_level(logging.DEBUG)
    c = admin.client
    responses: list[str] = []

    async def call(method, path, **kw):
        r = await c.request(method, path, **kw)
        responses.append(r.text)
        return r

    await _telegram(admin.sm)
    link = (await call("POST", "/api/live-links",
                       json={"name": "Reale", "kind": "t212_live", **T212})).json()
    lid = link["id"]
    await call("POST", f"/api/live-links/{lid}/test")
    await call("POST", f"/api/live-links/{lid}/dry-run", json={"on": True})
    await call("POST", f"/api/live-links/{lid}/arm", json={
        "totp": await admin.codes.code(), "confirm_name": "Reale", "accept_risks": True,
        "dedicated_account": True, "limits_set": True})
    pf = await _portfolio(c)
    await call("PUT", f"/api/portfolios/{pf['id']}/live-link",
               json={"link_id": lid, "totp": await admin.codes.code()})
    await call("GET", "/api/live-links")
    await call("GET", "/api/broker-links")
    await call("GET", f"/api/portfolios/{pf['id']}")
    await call("GET", "/api/portfolios")
    await call("GET", "/api/live/status")
    await call("GET", f"/api/broker-links/{lid}/positions")
    await call("POST", f"/api/broker-links/{lid}/reconcile")

    alpaca = (await call("POST", "/api/live-links", json={
        "name": "Reale A", "kind": "alpaca_live", "key_id": KEY_ID, "secret_key": SECRET,
        "crypto_enabled": False})).json()
    await call("POST", f"/api/live-links/{alpaca['id']}/test")

    # the broker answers 401 echoing the key, the secret and the Basic credential
    admin.fake.unauthorized = True
    for aid in (alpaca["id"], lid):
        r = await call("POST", f"/api/live-links/{aid}/test")
        assert r.status_code == 400 and r.json()["code"] == "PROVIDER_UNAVAILABLE", r.text
        await call("POST", f"/api/broker-links/{aid}/test")
        await call("GET", f"/api/broker-links/{aid}/positions")
    r = await call("POST", f"/api/live-links/{lid}/test")
    assert r.status_code == 400 and r.json()["code"] == "PROVIDER_UNAVAILABLE", r.text
    r = await call("POST", f"/api/broker-links/{lid}/test")
    assert r.json()["ok"] is False and r.json()["error"]
    await call("GET", f"/api/broker-links/{lid}/positions")
    await call("POST", f"/api/broker-links/{lid}/reconcile")
    await call("POST", f"/api/live-links/{lid}/disarm", json={})
    await call("GET", "/api/live-audit")
    await call("GET", "/api/live-audit.csv")
    await call("GET", "/api/live-links")

    seen = "\n".join(responses) + "\n" + await _all_rows_text(admin.sm) + "\n" + caplog.text
    for canary in CANARIES:
        assert canary not in seen, canary
    # Alpaca's error text reached the admin, masked (Trading 212 says only «credenziali»)
    assert "bad key ••••" in "\n".join(responses)


# -- DTOs, risk profile, CLI ---------------------------------------------------------------

async def test_portfolio_dto_mode_reale_and_live_state(admin):
    c = admin.client
    plain = await _portfolio(c)
    assert plain["mode"] == "simulazione" and plain["live_state"] is None
    link, pf = await _wizard(admin)
    got = (await c.get(f"/api/portfolios/{pf['id']}")).json()
    assert got["mode"] == "reale" and got["live_state"] == "armed"
    listed = {p["id"]: p for p in (await c.get("/api/portfolios")).json()}
    assert listed[pf["id"]]["mode"] == "reale" and listed[pf["id"]]["live_state"] == "armed"
    dash = (await c.get("/api/dashboard")).json()
    (item,) = [p for p in dash["portfolios"] if p["id"] == pf["id"]]
    assert item["mode"] == "reale" and item["live_state"] == "armed"
    assert dash["modes"]["reale"] == 1
    assert (await c.get("/api/dashboard/modes")).json()["reale"] == 1
    # dry run: orders are not sent, the portfolio is not "reale"
    await c.post(f"/api/live-links/{link['id']}/disarm", json={})
    assert (await c.post(f"/api/live-links/{link['id']}/dry-run",
                         json={"on": True})).status_code == 200
    got = (await c.get(f"/api/portfolios/{pf['id']}")).json()
    assert got["mode"] == "paper" and got["live_state"] == "dry_run"
    assert (await c.get("/api/dashboard/modes")).json()["reale"] == 0


async def test_risk_profile_fuse_editable_range(admin):
    c = admin.client
    pid = await _profile(c)
    profile = next(p for p in (await c.get("/api/risk-profiles")).json() if p["id"] == pid)
    assert profile["max_live_orders_per_hour"] == 30  # R8.1 default
    r = await c.patch(f"/api/risk-profiles/{pid}", json={"max_live_orders_per_hour": 5})
    assert r.status_code == 200 and r.json()["max_live_orders_per_hour"] == 5
    for bad in (0, 1001, -1, "x", None):
        r = await c.patch(f"/api/risk-profiles/{pid}", json={"max_live_orders_per_hour": bad})
        assert r.status_code == 400, bad
    for edge in (1, 1000):
        r = await c.patch(f"/api/risk-profiles/{pid}", json={"max_live_orders_per_hour": edge})
        assert r.status_code == 200 and r.json()["max_live_orders_per_hour"] == edge
    r = await c.post("/api/risk-profiles", json={"name": "Fuse", "max_live_orders_per_hour": 7})
    assert r.status_code == 200 and r.json()["max_live_orders_per_hour"] == 7
    r = await c.post("/api/risk-profiles", json={"name": "Bad", "max_live_orders_per_hour": 0})
    assert r.status_code == 400


async def _cli(admin, *argv):
    from smtrading import cli

    redis = Redis.from_url(admin.redis_url)
    try:
        return await cli.run_command(cli.build_parser().parse_args(list(argv)), admin.sm,
                                     None, redis)
    finally:
        await redis.aclose()


async def test_cli_live_disarm(admin):
    code, out = await _cli(admin, "live", "status")
    assert code == 0 and json.loads(out) == {"links": [], "message": "nessun collegamento live"}
    link, pf = await _wizard(admin)
    async with admin.sm() as s:  # an order never sent: rejected by the disarm
        s.add(Order(portfolio_id=uuid.UUID(pf["id"]), symbol="AAPL", side=OrderSide.buy,
                    type=OrderType.limit, qty=D(1), limit_price=D(50),
                    status=OrderStatus.pending, broker_status=BrokerState.TO_SEND,
                    broker_link_id=uuid.UUID(link["id"])))
        await s.commit()
    code, out = await _cli(admin, "live", "status")
    (row,) = json.loads(out)["links"]
    assert row["name"] == link["name"] and row["live_state"] == "armed"
    assert row["portfolio"] == pf["name"]
    code, out = await _cli(admin, "live", "disarm", link["name"], "--reason", "da shell")
    assert code == 0, out
    assert json.loads(out) == {"name": link["name"], "live_state": "configured",
                               "orders": 1}
    async with admin.sm() as s:
        db_link = await s.get(BrokerLink, uuid.UUID(link["id"]))
        assert db_link.live_state == "configured" and db_link.disarmed_reason == "da shell"
        row = await s.scalar(select(LiveAudit).where(LiveAudit.action == "disarmed"))
        assert row.actor == "cli"
    code, out = await _cli(admin, "live", "disarm", "Nessuno")
    assert code == 1 and json.loads(out)["error"]["code"] == "NOT_FOUND"
