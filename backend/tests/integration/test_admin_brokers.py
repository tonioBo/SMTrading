"""Admin API: broker links (masked config, paper/demo only), connection test, portfolio
link with the in-flight guard, reconciliations and broker positions."""

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.admin.common import redact
from smtrading.brokers import registry
from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.etoro_core import ETORO_URL
from smtrading.brokers.etoro_demo import EtoroDemoAdapter
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.service import NOT_DEDICATED_MESSAGE
from smtrading.brokers.live.t212_live import T212LiveAdapter
from smtrading.brokers.models import Reconciliation
from smtrading.brokers.service import get_link_config
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import BrokerState, Order, OrderSide, OrderType
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.positions.models import Position

D = Decimal
PASSWORD = "correct-horse-battery"
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
KEY_ID = "AKID-1234-5678"
SECRET = "SECRET-abcdefgh-9999"


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


class FakeAlpaca(AlpacaPaperAdapter):
    """The real constructor (paper URL check), canned network calls."""

    fail: ClassVar[str | None] = None
    positions: ClassVar[dict[str, Decimal]] = {}

    async def prepare(self, instruments=()) -> None:
        return None

    async def health_check(self) -> None:
        if type(self).fail:
            raise RuntimeError(type(self).fail)

    async def get_positions(self) -> dict[str, Decimal]:
        if type(self).fail:
            raise RuntimeError(type(self).fail)
        return dict(type(self).positions)


@pytest.fixture
def fake_alpaca(monkeypatch):
    FakeAlpaca.fail, FakeAlpaca.positions = None, {}
    monkeypatch.setitem(registry.ADAPTERS, "alpaca_paper", FakeAlpaca)
    return FakeAlpaca


async def _link(client, name="Alpaca", kind="alpaca_paper", config=None) -> dict:
    if config is None:
        config = {"key_id": KEY_ID, "secret_key": SECRET}
    r = await client.post("/api/broker-links", json={"name": name, "kind": kind, "config": config})
    assert r.status_code == 200, r.text
    return r.json()


async def _portfolio(client) -> dict:
    r = await client.post(
        "/api/portfolios", json={"name": f"P-{uuid.uuid4().hex[:8]}", "initial_cash": "10000"}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_requires_session(admin):
    transport = httpx.ASGITransport(app=admin.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.get("/api/broker-links")
    assert r.status_code == 401


async def test_create_and_list_masked(admin):
    link = await _link(admin.client)
    assert link["kind"] == "alpaca_paper" and link["enabled"] is True
    assert link["config_masked"] == {"key_id": "••••5678", "secret_key": "••••9999"}
    assert link["secret_keys"] == ["key_id", "secret_key"]
    assert link["base_url"] == "https://paper-api.alpaca.markets"
    assert link["status"] == "unknown" and link["portfolios"] == []

    r = await admin.client.get("/api/broker-links")
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [link["id"]]
    assert KEY_ID not in r.text and SECRET not in r.text

    t212 = await _link(admin.client, "T212", "t212_demo", {"api_key": "T212KEY-0001"})
    assert t212["secret_keys"] == ["api_key", "api_secret"]
    assert t212["base_url"] == "https://demo.trading212.com"
    assert t212["config_masked"] == {"api_key": "••••0001"}


async def test_patch_keeps_masked_and_disables(admin):
    link = await _link(admin.client)
    r = await admin.client.patch(f"/api/broker-links/{link['id']}", json={
        "config": {"key_id": "••••5678", "secret_key": "NEW-SECRET-7777"},
    })
    assert r.status_code == 200, r.text
    assert r.json()["config_masked"] == {"key_id": "••••5678", "secret_key": "••••7777"}
    async with admin.sm() as s:
        stored = await get_link_config(s, uuid.UUID(link["id"]))
    assert stored == {"key_id": KEY_ID, "secret_key": "NEW-SECRET-7777"}

    r = await admin.client.patch(f"/api/broker-links/{link['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = await admin.client.patch(
        f"/api/broker-links/{link['id']}", json={"config": {"secret_key": "••••0000"}}
    )
    assert r.status_code == 400  # a masked value that is not the stored one


async def test_non_paper_url_forbidden(admin):
    c = admin.client
    r = await c.post("/api/broker-links", json={
        "name": "Live", "kind": "alpaca_paper",
        "config": {"key_id": "k", "secret_key": "s", "base_url": AlpacaLiveAdapter.BASE_URL},
    })
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    r = await c.post("/api/broker-links", json={
        "name": "Live212", "kind": "t212_demo",
        "config": {"api_key": "k", "base_url": T212LiveAdapter.BASE_URL},
    })
    assert r.status_code == 403
    link = await _link(c)
    r = await c.patch(f"/api/broker-links/{link['id']}", json={
        "config": {"base_url": AlpacaLiveAdapter.BASE_URL},
    })
    assert r.status_code == 403
    assert (await c.get("/api/broker-links")).json()[0]["name"] == "Alpaca"


async def test_invalid_kind_and_keys(admin):
    c = admin.client
    r = await c.post("/api/broker-links", json={"name": "X", "kind": "revolutx", "config": {}})
    assert r.status_code == 400
    r = await c.post("/api/broker-links", json={
        "name": "X", "kind": "alpaca_paper", "config": {"key_id": "a", "password": "b"},
    })
    assert r.status_code == 400
    await _link(c)
    r = await c.post("/api/broker-links", json={"name": "Alpaca", "kind": "alpaca_paper",
                                                "config": {}})
    assert r.status_code == 400  # duplicate name


async def test_connection_test_records_status_and_redacts(admin, fake_alpaca):
    link = await _link(admin.client)
    fake_alpaca.fail = f"alpaca_paper: HTTP 401: bad key {SECRET}"
    r = await admin.client.post(f"/api/broker-links/{link['id']}/test")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and SECRET not in body["error"] and "HTTP 401" in body["error"]
    listed = (await admin.client.get("/api/broker-links")).json()[0]
    assert listed["status"] == "error" and SECRET not in (listed["last_error"] or "")

    fake_alpaca.fail = None
    r = await admin.client.post(f"/api/broker-links/{link['id']}/test")
    assert r.json()["ok"] is True
    listed = (await admin.client.get("/api/broker-links")).json()[0]
    assert listed["status"] == "ok" and listed["last_error"] is None


async def test_link_portfolio_and_unlink_guard(admin, fake_alpaca):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["broker_link_id"] == link["id"]
    listed = (await c.get("/api/broker-links")).json()[0]
    assert listed["portfolios"] == [{"id": pf["id"], "name": pf["name"]}]

    async with admin.sm() as s:
        order = await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=uuid.UUID(pf["id"]), symbol="AAPL", side=OrderSide.buy,
            type=OrderType.market, qty=D(1),
        ), now=OPEN)
        await s.commit()
    assert order.broker_status == BrokerState.TO_SEND

    orders = (await c.get(f"/api/portfolios/{pf['id']}/orders")).json()
    assert orders[0]["broker_link_id"] == link["id"]
    assert orders[0]["broker_replicated"] is False  # not at the broker yet (BrokerSync)
    assert orders[0]["broker_status"] == "to_send" and orders[0]["broker_order_id"] is None

    async with admin.sm() as s:  # BrokerSync claimed it: the request may be at the broker
        o = await s.get(Order, order.id)
        o.broker_status, o.broker_sent_at = BrokerState.SENDING, OPEN
        await s.commit()
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link", json={"broker_link_id": None})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER"
    assert (await c.get(f"/api/portfolios/{pf['id']}")).json()["broker_link_id"] == link["id"]

    async with admin.sm() as s:  # never sent: unlinking hands it back to the simulator
        o = await s.get(Order, order.id)
        o.broker_status, o.broker_sent_at = BrokerState.TO_SEND, None
        await s.commit()
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link", json={"broker_link_id": None})
    assert r.status_code == 200 and r.json()["broker_link_id"] is None
    listed = (await c.get("/api/broker-links")).json()[0]
    assert listed["portfolios"] == []


async def test_link_to_disabled_or_missing_link_refused(admin):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    await c.patch(f"/api/broker-links/{link['id']}", json={"enabled": False})
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code == 400
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": str(uuid.uuid4())})
    assert r.status_code == 404


async def test_reconcile_now_history_and_idempotency(admin, fake_alpaca):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    await c.put(f"/api/portfolios/{pf['id']}/broker-link", json={"broker_link_id": link["id"]})
    fake_alpaca.positions = {"AAPL": D(3)}

    headers = {"Idempotency-Key": "rec-1"}
    r1 = await c.post(f"/api/broker-links/{link['id']}/reconcile", headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = await c.post(f"/api/broker-links/{link['id']}/reconcile", headers=headers)
    assert r2.json() == r1.json()
    rows = r1.json()["reconciliations"]
    assert len(rows) == 1 and rows[0]["ok"] is False
    assert rows[0]["diffs"] == [{"symbol": "AAPL", "ours": "0", "broker": "3"}]
    async with admin.sm() as s:
        assert await s.scalar(select(func.count()).select_from(Reconciliation)) == 1

    fake_alpaca.positions = {}
    r = await c.post(f"/api/broker-links/{link['id']}/reconcile")
    assert r.json()["reconciliations"][0]["ok"] is True

    r = await c.get(f"/api/broker-links/{link['id']}/reconciliations?limit=10")
    assert r.status_code == 200
    history = r.json()
    assert [h["ok"] for h in history] == [True, False]  # newest first
    assert history[0]["portfolio_name"] == pf["name"]
    r = await c.get(f"/api/broker-links/{link['id']}/reconciliations?limit=1")
    assert len(r.json()) == 1


async def test_reconcile_failure_redacted_and_recorded(admin, fake_alpaca):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    await c.put(f"/api/portfolios/{pf['id']}/broker-link", json={"broker_link_id": link["id"]})
    fake_alpaca.fail = f"boom {SECRET}"
    r = await c.post(f"/api/broker-links/{link['id']}/reconcile")
    assert r.status_code == 400 and r.json()["code"] == "PROVIDER_UNAVAILABLE"
    assert SECRET not in r.text
    listed = (await c.get("/api/broker-links")).json()[0]
    assert listed["status"] == "error" and SECRET not in listed["last_error"]


async def test_broker_positions(admin, fake_alpaca):
    link = await _link(admin.client)
    fake_alpaca.positions = {"MSFT": D("2.5"), "AAPL": D(1)}
    r = await admin.client.get(f"/api/broker-links/{link['id']}/positions")
    assert r.status_code == 200, r.text
    assert r.json() == {"positions": [
        {"symbol": "AAPL", "qty": "1"}, {"symbol": "MSFT", "qty": "2.5"},
    ]}
    r = await admin.client.get(f"/api/broker-links/{uuid.uuid4()}/positions")
    assert r.status_code == 404


async def test_portfolio_mode_follows_the_broker_link(admin, fake_alpaca):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    assert (await c.get(f"/api/portfolios/{pf['id']}")).json()["mode"] == "simulazione"
    dash = (await c.get("/api/dashboard")).json()
    sim_before = dash["modes"]["simulazione"]
    assert dash["modes"]["paper"] == 0 and dash["modes"]["reale"] == 0
    assert {p["id"]: p["mode"] for p in dash["portfolios"]}[pf["id"]] == "simulazione"

    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    listed = {p["id"]: p["mode"] for p in (await c.get("/api/portfolios")).json()}
    assert listed[pf["id"]] == "paper"
    dash = (await c.get("/api/dashboard")).json()
    assert dash["modes"] == {"simulazione": sim_before - 1, "paper": 1, "reale": 0}
    assert {p["id"]: p["mode"] for p in dash["portfolios"]}[pf["id"]] == "paper"


async def test_dashboard_mode_counts(admin, fake_alpaca):
    """Seeded Demo + 1 more simulated, 1 on Alpaca paper: counts grouped by mode_label."""
    c = admin.client
    link = await _link(c)
    await _portfolio(c)
    pf = await _portfolio(c)
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code == 200 and r.json()["mode_label"] == "Alpaca · prova"
    assert r.json()["broker_kind"] == "alpaca_paper"
    dash = (await c.get("/api/dashboard")).json()
    assert dash["mode_counts"] == [
        {"label": "SMTrading (simulato)", "count": 2, "live": False},
        {"label": "Alpaca · prova", "count": 1, "live": False},
    ]
    row = {p["id"]: p for p in dash["portfolios"]}[pf["id"]]
    assert (row["mode"], row["broker_kind"], row["mode_label"]) == (
        "paper", "alpaca_paper", "Alpaca · prova")
    assert dash["modes"] == {"simulazione": 2, "paper": 1, "reale": 0}
    assert (await c.get("/api/dashboard/modes")).json() == dash["modes"]


class SMTFailAlpaca(FakeAlpaca):
    """The adapter itself raises an SMTError whose text carries the key."""

    async def get_positions(self) -> dict[str, Decimal]:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, f"alpaca_paper: bad key {SECRET}")


async def test_broker_smterror_redacted_and_recorded(admin, monkeypatch):
    monkeypatch.setitem(registry.ADAPTERS, "alpaca_paper", SMTFailAlpaca)
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    await c.put(f"/api/portfolios/{pf['id']}/broker-link", json={"broker_link_id": link["id"]})
    for call in (lambda: c.get(f"/api/broker-links/{link['id']}/positions"),
                 lambda: c.post(f"/api/broker-links/{link['id']}/reconcile")):
        r = await call()
        assert r.status_code == 400 and r.json()["code"] == "PROVIDER_UNAVAILABLE"
        assert SECRET not in r.text and "bad key" in r.text
        listed = (await c.get("/api/broker-links")).json()[0]
        assert listed["status"] == "error"
        assert "bad key" in listed["last_error"] and SECRET not in listed["last_error"]
        await c.post(f"/api/broker-links/{link['id']}/test")  # back to ok (health passes)


def test_t212_basic_credential_is_redacted():
    from smtrading.admin.routes_brokers import link_secrets

    config = {"api_key": "K-12345678", "api_secret": "S-87654321"}
    encoded = base64.b64encode(b"K-12345678:S-87654321").decode()
    secrets = link_secrets("t212_demo", config)
    assert encoded in secrets
    text = redact(f"401 for Authorization: Basic {encoded}", secrets)
    assert encoded not in text and "K-12345678" not in text
    assert set(link_secrets("alpaca_paper", {"key_id": "A", "secret_key": "B"})) == {"A", "B"}


async def test_broker_mutations_require_csrf(admin):
    link = await _link(admin.client)
    pf = await _portfolio(admin.client)
    transport = httpx.ASGITransport(app=admin.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies=admin.client.cookies) as no_csrf:
        calls = [
            no_csrf.post("/api/broker-links", json={"name": "X", "kind": "alpaca_paper",
                                                    "config": {}}),
            no_csrf.patch(f"/api/broker-links/{link['id']}", json={"enabled": False}),
            no_csrf.post(f"/api/broker-links/{link['id']}/test"),
            no_csrf.post(f"/api/broker-links/{link['id']}/reconcile"),
            no_csrf.put(f"/api/portfolios/{pf['id']}/broker-link",
                        json={"broker_link_id": link["id"]}),
        ]
        for call in calls:
            r = await call
            assert r.status_code == 403 and r.json()["message"] == "CSRF"
    listed = (await admin.client.get("/api/broker-links")).json()
    assert len(listed) == 1 and listed[0]["enabled"] is True and listed[0]["portfolios"] == []


async def test_link_refused_with_open_positions(admin, fake_alpaca):
    c = admin.client
    link = await _link(c)
    pf = await _portfolio(c)
    async with admin.sm() as s:
        s.add(Position(portfolio_id=uuid.UUID(pf["id"]), symbol="AAPL", qty=D(2),
                       avg_price=D("200")))
        await s.commit()
    r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                    json={"broker_link_id": link["id"]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER"
    assert "Chiudi prima le posizioni aperte" in r.json()["message"]
    assert (await c.get(f"/api/portfolios/{pf['id']}")).json()["broker_link_id"] is None


async def test_positions_success_clears_the_link_error(admin, fake_alpaca):
    """N5: a successful broker read records the link as ok again."""
    link = await _link(admin.client)
    fake_alpaca.fail = "boom"
    assert (await admin.client.get(f"/api/broker-links/{link['id']}/positions")).status_code == 400
    assert (await admin.client.get("/api/broker-links")).json()[0]["status"] == "error"
    fake_alpaca.fail = None
    assert (await admin.client.get(f"/api/broker-links/{link['id']}/positions")).status_code == 200
    listed = (await admin.client.get("/api/broker-links")).json()[0]
    assert listed["status"] == "ok" and listed["last_error"] is None


ETORO_KEYS = {"api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-0002"}
ETORO_PNL = ETORO_URL + EtoroDemoAdapter.INFO_V1 + "/pnl"


def _pnl(positions=(), orders=()) -> httpx.Response:
    return httpx.Response(200, json={"clientPortfolio": {
        "credit": 1000, "accountCurrencyId": 1, "positions": list(positions),
        "orders": list(orders), "ordersForOpen": [], "ordersForClose": [], "mirrors": []}})


async def test_create_etoro_demo_link_keys_masked(admin):
    c = admin.client
    link = await _link(c, "eToro", "etoro_demo", dict(ETORO_KEYS))
    assert link["kind"] == "etoro_demo" and link["base_url"] == ETORO_URL
    assert link["secret_keys"] == ["api_key", "user_key"]
    assert link["config_masked"] == {"api_key": "••••0001", "user_key": "••••0002"}
    listed = await c.get("/api/broker-links")
    assert all(v not in listed.text for v in ETORO_KEYS.values())
    async with admin.sm() as s:
        assert await get_link_config(s, uuid.UUID(link["id"])) == ETORO_KEYS

    r = await c.post("/api/broker-links", json={
        "name": "X", "kind": "etoro_demo", "config": {**ETORO_KEYS, "api_secret": "s"}})
    assert r.status_code == 400 and r.json()["details"]["field"] == "config"
    r = await c.post("/api/broker-links", json={
        "name": "Y", "kind": "etoro_demo",
        "config": {**ETORO_KEYS, "base_url": "https://elsewhere.example.com"}})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    r = await c.post("/api/broker-links", json={"name": "Z", "kind": "revolut", "config": {}})
    assert r.status_code == 400 and "etoro_demo" in r.json()["message"]


async def test_link_refused_when_demo_account_not_empty(admin):
    """Linking a portfolio to an eToro demo link reads the account (GET only) first: a
    position or an open order refuses it with NOT_DEDICATED; an empty account links it.
    A second portfolio on a link already in use is not refused (the account is in use)."""
    c = admin.client
    link = await _link(c, "eToro", "etoro_demo", dict(ETORO_KEYS))
    pf = await _portfolio(c)
    position = {"positionID": 501, "instrumentID": 1001, "isBuy": True, "units": 2,
                "openRate": 190, "amount": 380, "leverage": 1, "mirrorID": 0,
                "settlementTypeID": 1, "totalFees": 0}
    with respx.mock(assert_all_mocked=True) as mock:
        pnl = mock.get(ETORO_PNL)
        for state in ({"positions": [position]},
                      {"orders": [{"orderID": 9001, "instrumentID": 1002, "amount": 25}]}):
            pnl.mock(return_value=_pnl(**state))
            r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                            json={"broker_link_id": link["id"]})
            assert r.status_code == 400, r.text
            body = r.json()
            assert (body["code"], body["message"]) == ("INVALID_ORDER", NOT_DEDICATED_MESSAGE)
            assert body["details"]["field"] == "account"
            assert (await c.get(f"/api/portfolios/{pf['id']}")).json()["broker_link_id"] is None

        pnl.mock(return_value=_pnl())
        r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                        json={"broker_link_id": link["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["broker_link_id"] == link["id"]
        assert r.json()["mode_label"] == "eToro · demo"
        assert {call.request.method for call in mock.calls} == {"GET"}
        assert pnl.call_count == 3

        pnl.mock(return_value=_pnl(positions=[position]))  # the first portfolio's position
        other = await _portfolio(c)
        r = await c.put(f"/api/portfolios/{other['id']}/broker-link",
                        json={"broker_link_id": link["id"]})
        assert r.status_code == 200, r.text
        assert pnl.call_count == 3  # the account is in use: not read again


async def test_link_demo_account_read_failure_is_redacted(admin):
    c = admin.client
    link = await _link(c, "eToro", "etoro_demo", dict(ETORO_KEYS))
    pf = await _portfolio(c)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(ETORO_PNL).mock(return_value=httpx.Response(
            401, json={"errorMessage": f"bad key {ETORO_KEYS['user_key']}"}))
        r = await c.put(f"/api/portfolios/{pf['id']}/broker-link",
                        json={"broker_link_id": link["id"]})
    assert r.status_code == 400 and r.json()["code"] == "PROVIDER_UNAVAILABLE"
    assert all(v not in r.text for v in ETORO_KEYS.values())
    assert (await c.get(f"/api/portfolios/{pf['id']}")).json()["broker_link_id"] is None


# -- «Prova collegamento» (guided test) ----------------------------------------------


class FakeAlpacaGuided(FakeAlpaca):
    placed: ClassVar[list] = []
    canceled: ClassVar[list] = []

    async def get_account(self):
        from smtrading.brokers.base import AccountSnapshot

        return AccountSnapshot("PA-ACCOUNT-0042", "cash", "USD", D("100000"), 0, 0,
                               None, None, {})

    async def place_order(self, order, instrument):
        from smtrading.brokers.base import BrokerOrder

        type(self).placed.append(order)
        return BrokerOrder("bo-guided", "accepted", D(0), None, None)

    async def cancel_order(self, broker_order_id):
        type(self).canceled.append(broker_order_id)

    async def get_order(self, broker_order_id):
        from smtrading.brokers.base import BrokerOrder

        return BrokerOrder(broker_order_id, "canceled", D(0), None, None)


@pytest.fixture
def guided_alpaca(monkeypatch):
    FakeAlpacaGuided.fail, FakeAlpacaGuided.placed, FakeAlpacaGuided.canceled = None, [], []
    monkeypatch.setitem(registry.ADAPTERS, "alpaca_paper", FakeAlpacaGuided)
    return FakeAlpacaGuided


async def _actions(admin, route_part: str) -> list[str]:
    from smtrading.useractivity.models import UserActivity

    async with admin.sm() as s:
        return list(await s.scalars(
            select(UserActivity.action).where(UserActivity.route.contains(route_part),
                                              UserActivity.provisional.is_(False))
            .order_by(UserActivity.id)))


async def test_guided_test_read_only_steps(admin, guided_alpaca):
    link = await _link(admin.client)
    r = await admin.client.post(f"/api/broker-links/{link['id']}/guided-test", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert [s["key"] for s in body["steps"]] == [
        "auth", "account", "buying_power", "market", "quote"]
    assert all(s["status"] == "ok" for s in body["steps"])
    assert guided_alpaca.placed == []  # read-only without the order step
    assert "PA-ACCOUNT-0042" not in r.text and KEY_ID not in r.text
    links = (await admin.client.get("/api/broker-links")).json()
    assert links[0]["status"] == "ok"
    assert await _actions(admin, "guided-test") == ["brokers.guided_test"]


async def test_guided_test_order_needs_the_second_confirmation(admin, guided_alpaca):
    link = await _link(admin.client)
    url = f"/api/broker-links/{link['id']}/guided-test"
    for confirm in (None, "si", "ordine"):
        r = await admin.client.post(url, json={"order_test": True, "confirm": confirm})
        assert r.status_code == 400 and r.json()["details"]["field"] == "confirm"
    assert guided_alpaca.placed == []
    r = await admin.client.post(url, json={"order_test": True, "confirm": "ORDINE DI PROVA"})
    assert r.status_code == 200, r.text
    order = r.json()["steps"][-1]
    assert order["key"] == "order" and order["status"] == "ok", order
    (placed,) = guided_alpaca.placed
    assert placed.type == OrderType.limit and placed.limit_price == D("100.02")
    assert guided_alpaca.canceled == ["bo-guided"]
    async with admin.sm() as s:  # nothing local: the test order lives at the broker only
        assert await s.scalar(select(func.count()).select_from(Order)) == 0
    assert (await _actions(admin, "guided-test"))[-1] == "brokers.guided_test_order"


async def test_guided_test_order_skipped_with_the_kill_switch(admin, guided_alpaca):
    from smtrading.risk.settings import set_kill_switch

    link = await _link(admin.client)
    async with admin.sm() as s:
        await set_kill_switch(s, True)
        await s.commit()
    r = await admin.client.post(f"/api/broker-links/{link['id']}/guided-test",
                                json={"order_test": True, "confirm": "ORDINE DI PROVA"})
    assert r.status_code == 200
    assert r.json()["steps"][-1]["status"] == "skipped"
    assert guided_alpaca.placed == []


async def test_guided_test_refused_on_a_real_money_link(admin, guided_alpaca):
    from smtrading.brokers.models import BrokerLink

    async with admin.sm() as s:
        live = BrokerLink(name="Reale", kind="alpaca_live", live_state="armed")
        s.add(live)
        await s.commit()
    r = await admin.client.post(f"/api/broker-links/{live.id}/guided-test",
                                json={"order_test": True, "confirm": "ORDINE DI PROVA"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert "denaro reale" in r.json()["message"]
    assert guided_alpaca.placed == []


async def test_guided_test_unknown_symbol(admin, guided_alpaca):
    link = await _link(admin.client)
    r = await admin.client.post(f"/api/broker-links/{link['id']}/guided-test",
                                json={"symbol": "NOPE"})
    assert r.status_code == 400 and r.json()["details"]["field"] == "symbol"


async def test_guided_test_failed_auth_records_the_link_error(admin, guided_alpaca):
    guided_alpaca.fail = f"HTTP 403: bad key {SECRET}"
    link = await _link(admin.client)
    r = await admin.client.post(f"/api/broker-links/{link['id']}/guided-test", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["steps"][0]["status"] == "error"
    assert SECRET not in r.text
    links = (await admin.client.get("/api/broker-links")).json()
    assert links[0]["status"] == "error" and SECRET not in (links[0]["last_error"] or "")
