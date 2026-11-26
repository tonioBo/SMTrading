"""Admin API of intraday work (mode A and the mode B switch): the portfolio settings and
their checks (admin only, audited; the auto-rules switch reads the broker account with no
transaction open; one mode per portfolio; day leverage at most 2), the trigger log, the
account state per broker link, the day quantity of open positions, the risk profile's
day-close margin and the CLI of the drill strategy (invisible to the agents)."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.alpaca_paper import PAPER_URL
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal
PASSWORD = "correct-horse-battery"
ACCT_ID = "acct-id-canary-5520"
KEY = "KID-CANARY-7731"
STATE_KEYS = {"buying_power", "non_marginable_buying_power", "multiplier", "equity",
              "trading_blocked", "account_blocked", "read_at"}


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    """A logged-in admin client on a seeded fresh database; `viewer()` opens a viewer."""
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
    qs.set_fx("USD", "EUR", D("0.8"))
    qs.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    transport = httpx.ASGITransport(app=app)
    viewers: list[httpx.AsyncClient] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            async def viewer() -> httpx.AsyncClient:
                v_email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
                async with sm() as s:
                    s.add(AdminUser(email=v_email, password_hash=await hash_password(PASSWORD),
                                    role="viewer"))
                    await s.commit()
                c = httpx.AsyncClient(transport=transport, base_url="http://test")
                viewers.append(c)
                r = await c.post("/api/auth/login",
                                 json={"email": v_email, "password": PASSWORD})
                assert r.status_code == 200, r.text
                c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
                return c

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.qs, ns.email, ns.db_url = client, sm, qs, email, fresh_db
            ns.redis_url, ns.viewer = redis_url, viewer
            yield ns
    finally:
        for c in viewers:
            await c.aclose()
        stop.set()
        await lifespan
        await engine.dispose()


@pytest.fixture
def broker():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as r:
        yield r


def _account(**kw):
    base = {"id": ACCT_ID, "cash": "30000", "buying_power": "60000", "currency": "USD",
            "non_marginable_buying_power": "30000", "equity": "30000", "multiplier": "2",
            "trading_blocked": False, "account_blocked": False}
    base.update(kw)
    return base


def _mock_account(router, side_effect=None, **kw):
    router.get(f"{PAPER_URL}/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{PAPER_URL}/v2/orders").mock(return_value=httpx.Response(200, json=[]))
    route = router.get(f"{PAPER_URL}/v2/account")
    if side_effect is not None:
        return route.mock(side_effect=side_effect)
    return route.mock(return_value=httpx.Response(200, json=_account(**kw)))


async def _portfolio(admin, *, link_kind=None, enabled=True, **fields) -> str:
    """A portfolio (optionally linked to a new link of `link_kind`), `fields` set in the
    database."""
    from smtrading.brokers.models import BrokerLink
    from smtrading.brokers.service import create_link
    from smtrading.portfolios.models import Portfolio

    r = await admin.client.post("/api/portfolios", json={
        "name": f"P-{uuid.uuid4().hex[:8]}", "initial_cash": "10000"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    async with admin.sm() as s:
        p = await s.get(Portfolio, uuid.UUID(pid))
        if link_kind == "alpaca_paper":
            link = await create_link(s, f"Conto {uuid.uuid4().hex[:6]}", "alpaca_paper",
                                     {"key_id": KEY, "secret_key": "SEC-CANARY"})
            link.enabled = enabled
        elif link_kind is not None:
            link = BrokerLink(name=f"Conto {uuid.uuid4().hex[:6]}", kind=link_kind,
                              enabled=enabled)
            s.add(link)
        if link_kind is not None:
            await s.flush()
            p.broker_link_id = link.id
        for k, v in fields.items():
            setattr(p, k, v)
        await s.commit()
    return pid


async def _row(admin, pid):
    from smtrading.portfolios.models import Portfolio

    async with admin.sm() as s:
        return await s.get(Portfolio, uuid.UUID(pid))


async def _link_of(admin, pid):
    from smtrading.brokers.models import BrokerLink

    p = await _row(admin, pid)
    async with admin.sm() as s:
        return await s.get(BrokerLink, p.broker_link_id)


async def _audits(admin, method):
    from smtrading.agents.models import McpAuditLog

    async with admin.sm() as s:
        return list(await s.scalars(
            select(McpAuditLog).where(McpAuditLog.method == method).order_by(McpAuditLog.id)))


def _refused(r, code="INVALID_ORDER", text=None):
    assert 400 <= r.status_code < 600, r.text
    body = r.json()
    err = body.get("error", body)
    assert err["code"] == code, body
    if text is not None:
        assert text in err["message"], err["message"]
    return err


async def _agent(admin, pid, permission, *, active=True):
    from smtrading.agents import service as agents
    from smtrading.agents.models import Agent

    async with admin.sm() as s:
        agent, _ = await agents.create_agent(s, f"agente-{uuid.uuid4().hex[:6]}")
        await s.flush()
        await agents.assign_portfolio(s, agent.id, uuid.UUID(pid), permission)
        (await s.get(Agent, agent.id)).active = active
        await s.commit()
        return str(agent.id)


# --- settings ----------------------------------------------------------------------------


async def test_patch_intraday_fields_admin_only(admin):
    pid = await _portfolio(admin)
    viewer = await admin.viewer()
    r = await viewer.patch(f"/api/portfolios/{pid}", json={"intraday_enabled": True})
    assert r.status_code == 403, r.text
    assert (await _row(admin, pid)).intraday_enabled is False

    got = (await admin.client.get(f"/api/portfolios/{pid}")).json()
    assert (got["intraday_enabled"], got["auto_rules_enabled"]) == (False, False)
    assert got["intraday_timeframes"] == ["15m", "1h"]
    assert got["intraday_wake_agent_id"] is None and got["day_close_minutes"] == 10
    assert got["max_day_trades_5d"] is None and got["intraday_max_wakes_per_hour"] == 6

    r = await admin.client.patch(f"/api/portfolios/{pid}", json={
        "intraday_enabled": True, "intraday_timeframes": ["5m"], "day_close_minutes": 15,
        "max_day_trades_5d": 3, "intraday_max_wakes_per_hour": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intraday_enabled"] is True and body["intraday_timeframes"] == ["5m"]
    assert (body["day_close_minutes"], body["max_day_trades_5d"],
            body["intraday_max_wakes_per_hour"]) == (15, 3, 4)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"max_day_trades_5d": None})
    assert r.status_code == 200 and r.json()["max_day_trades_5d"] is None


@pytest.mark.parametrize("body", [
    {"intraday_timeframes": ["1d"]}, {"intraday_timeframes": []},
    {"intraday_timeframes": ["5m", "5m"]}, {"intraday_timeframes": None},
    {"day_close_minutes": 1}, {"day_close_minutes": 61}, {"day_close_minutes": None},
    {"max_day_trades_5d": -1}, {"max_day_trades_5d": 101},
    {"intraday_max_wakes_per_hour": 0}, {"intraday_max_wakes_per_hour": 61},
    {"intraday_enabled": None}, {"auto_rules_enabled": None},
])
async def test_patch_rejects_bad_timeframes(admin, body):
    """Bad timeframes (and the other bounds): INVALID_ORDER, nothing changes."""
    pid = await _portfolio(admin)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json=body)
    _refused(r)
    p = await _row(admin, pid)
    assert p.intraday_timeframes == ["15m", "1h"] and p.day_close_minutes == 10
    assert await _audits(admin, "admin:portfolio.intraday") == []


async def test_patch_rejects_read_agent(admin):
    pid = await _portfolio(admin)
    msg = "l'agente da svegliare deve avere il permesso trade su questo portafoglio"
    reader = await _agent(admin, pid, "read")
    _refused(await admin.client.patch(f"/api/portfolios/{pid}",
                                      json={"intraday_wake_agent_id": reader}), text=msg)
    inactive = await _agent(admin, pid, "trade", active=False)
    _refused(await admin.client.patch(f"/api/portfolios/{pid}",
                                      json={"intraday_wake_agent_id": inactive}), text=msg)
    other = await _portfolio(admin)
    elsewhere = await _agent(admin, other, "trade")
    _refused(await admin.client.patch(f"/api/portfolios/{pid}",
                                      json={"intraday_wake_agent_id": elsewhere}), text=msg)
    _refused(await admin.client.patch(f"/api/portfolios/{pid}",
                                      json={"intraday_wake_agent_id": str(uuid.uuid4())}),
             text=msg)
    trader = await _agent(admin, pid, "trade")
    r = await admin.client.patch(f"/api/portfolios/{pid}",
                                 json={"intraday_wake_agent_id": trader})
    assert r.status_code == 200, r.text
    assert r.json()["intraday_wake_agent_id"] == trader
    r = await admin.client.patch(f"/api/portfolios/{pid}",
                                 json={"intraday_wake_agent_id": None})
    assert r.status_code == 200 and r.json()["intraday_wake_agent_id"] is None


async def test_patch_audited(admin):
    pid = await _portfolio(admin)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"name": "Solo nome"})
    assert r.status_code == 200
    assert await _audits(admin, "admin:portfolio.intraday") == []
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={
        "intraday_enabled": True, "intraday_timeframes": ["15m"], "name": "Nome nuovo"})
    assert r.status_code == 200, r.text
    (audit,) = await _audits(admin, "admin:portfolio.intraday")
    assert audit.agent_id is None and audit.outcome == "ok"
    assert audit.params["portfolio_id"] == pid and audit.params["by"] == admin.email
    assert audit.params["changes"] == {"intraday_enabled": True,
                                       "intraday_timeframes": ["15m"]}


# --- the auto-rules switch ------------------------------------------------------------------


async def test_auto_rules_allowed_on_multiplier_one_account(admin, broker):
    """Spec point 7: every Alpaca account is a margin account; multiplier 1 only caps the
    leverage, so mode B is allowed. The account is read with no transaction open."""
    pid = await _portfolio(admin, link_kind="alpaca_paper")
    probe = make_engine(admin.db_url)
    open_tx: list[int] = []

    async def account(request):
        async with probe.connect() as conn:
            open_tx.append(await conn.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'")))
        return httpx.Response(200, json=_account(multiplier="1", equity="30000",
                                                 buying_power="30000"))

    route = _mock_account(broker, side_effect=account)
    try:
        r = await admin.client.patch(f"/api/portfolios/{pid}",
                                     json={"auto_rules_enabled": True})
    finally:
        await probe.dispose()
    assert r.status_code == 200, r.text
    assert r.json()["auto_rules_enabled"] is True
    assert route.call_count == 1 and open_tx == [0]
    link = await _link_of(admin, pid)
    assert set(link.pdt_state) == STATE_KEYS and link.pdt_state["multiplier"] == "1"


@pytest.mark.parametrize("flags", [{"trading_blocked": True}, {"account_blocked": True}])
async def test_auto_rules_refused_on_blocked_account(admin, broker, flags):
    pid = await _portfolio(admin, link_kind="alpaca_paper")
    _mock_account(broker, multiplier="4", **flags)
    link = await _link_of(admin, pid)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": True})
    _refused(r, text=f"Regole automatiche non attivabili: il conto {link.name} risulta "
                     "bloccato dal broker")
    assert (await _row(admin, pid)).auto_rules_enabled is False
    assert await _audits(admin, "admin:portfolio.intraday") == []


async def test_auto_rules_allowed_on_small_margin_account(admin, broker):
    """No equity threshold (the PDT rule is gone)."""
    pid = await _portfolio(admin, link_kind="alpaca_paper", leverage=D(2))
    _mock_account(broker, multiplier="4", equity="10000")
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": True})
    assert r.status_code == 200, r.text
    assert (await _row(admin, pid)).auto_rules_enabled is True
    (audit,) = await _audits(admin, "admin:portfolio.intraday")
    assert audit.params["changes"] == {"auto_rules_enabled": True}
    assert ACCT_ID not in json.dumps(audit.params) and KEY not in json.dumps(audit.params)
    link = await _link_of(admin, pid)
    assert set(link.pdt_state) == STATE_KEYS and link.pdt_state["equity"] == "10000"
    assert ACCT_ID not in json.dumps(link.pdt_state)
    assert ACCT_ID not in r.text


async def test_auto_rules_refused_without_alpaca_link(admin):
    msg = "Regole automatiche disponibili solo su portafogli collegati ad Alpaca"
    simulated = await _portfolio(admin)
    _refused(await admin.client.patch(f"/api/portfolios/{simulated}",
                                      json={"auto_rules_enabled": True}), text=msg)
    disabled = await _portfolio(admin, link_kind="alpaca_paper", enabled=False)
    _refused(await admin.client.patch(f"/api/portfolios/{disabled}",
                                      json={"auto_rules_enabled": True}), text=msg)
    etoro = await _portfolio(admin, link_kind="etoro_live")
    _refused(await admin.client.patch(f"/api/portfolios/{etoro}",
                                      json={"auto_rules_enabled": True}), text=msg)
    for pid in (simulated, disabled, etoro):
        assert (await _row(admin, pid)).auto_rules_enabled is False


async def test_auto_rules_read_failure_refused(admin, broker):
    pid = await _portfolio(admin, link_kind="alpaca_paper")
    _mock_account(broker, side_effect=lambda request: httpx.Response(500, json={}))
    link = await _link_of(admin, pid)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": True})
    _refused(r, "PROVIDER_UNAVAILABLE", text=f"Impossibile leggere il conto {link.name}: riprova")
    assert (await _row(admin, pid)).auto_rules_enabled is False


async def test_auto_rules_disable_always_allowed(admin, broker):
    """Turning it off never reads the broker (no route is mocked: a call would fail)."""
    pid = await _portfolio(admin, link_kind="alpaca_paper", auto_rules_enabled=True)
    r = await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": False})
    assert r.status_code == 200, r.text
    assert (await _row(admin, pid)).auto_rules_enabled is False
    # already on: sending true again reads nothing either
    await admin.client.patch(f"/api/portfolios/{pid}", json={"auto_rules_enabled": True})
    pid2 = await _portfolio(admin, link_kind="alpaca_paper", auto_rules_enabled=True)
    r = await admin.client.patch(f"/api/portfolios/{pid2}", json={"auto_rules_enabled": True})
    assert r.status_code == 200, r.text


async def test_one_mode_per_portfolio(admin, broker):
    """Spec point 8: intraday_enabled and auto_rules_enabled never on together."""
    msg = "un solo modo"
    route = _mock_account(broker, multiplier="4")
    both = await _portfolio(admin, link_kind="alpaca_paper")
    _refused(await admin.client.patch(f"/api/portfolios/{both}", json={
        "intraday_enabled": True, "auto_rules_enabled": True}), text=msg)
    mode_a = await _portfolio(admin, link_kind="alpaca_paper", intraday_enabled=True)
    _refused(await admin.client.patch(f"/api/portfolios/{mode_a}",
                                      json={"auto_rules_enabled": True}), text=msg)
    mode_b = await _portfolio(admin, link_kind="alpaca_paper", auto_rules_enabled=True)
    _refused(await admin.client.patch(f"/api/portfolios/{mode_b}",
                                      json={"intraday_enabled": True}), text=msg)
    assert route.call_count == 0  # refused before reading the broker
    for pid in (both, mode_a, mode_b):
        p = await _row(admin, pid)
        assert not (p.intraday_enabled and p.auto_rules_enabled)
    # switching mode in one request is fine
    r = await admin.client.patch(f"/api/portfolios/{mode_a}", json={
        "intraday_enabled": False, "auto_rules_enabled": True})
    assert r.status_code == 200, r.text
    p = await _row(admin, mode_a)
    assert (p.intraday_enabled, p.auto_rules_enabled) == (False, True)


async def test_day_switch_refused_over_leverage_two(admin, broker):
    msg = "Per il lavoro in giornata la leva del portafoglio può essere al massimo 2"
    route = _mock_account(broker, multiplier="4")
    high = await _portfolio(admin, link_kind="alpaca_paper", leverage=D(3))
    _refused(await admin.client.patch(f"/api/portfolios/{high}",
                                      json={"intraday_enabled": True}), text=msg)
    _refused(await admin.client.patch(f"/api/portfolios/{high}",
                                      json={"auto_rules_enabled": True}), text=msg)
    assert route.call_count == 0
    # lowering the leverage in the same request is fine
    r = await admin.client.patch(f"/api/portfolios/{high}",
                                 json={"intraday_enabled": True, "leverage": "2"})
    assert r.status_code == 200, r.text
    _refused(await admin.client.patch(f"/api/portfolios/{high}", json={"leverage": "2.5"}),
             text=msg)
    assert (await _row(admin, high)).leverage == D(2)
    r = await admin.client.patch(f"/api/portfolios/{high}", json={"leverage": "1.5"})
    assert r.status_code == 200, r.text
    # a portfolio with both switches off keeps the general limits
    plain = await _portfolio(admin)
    r = await admin.client.patch(f"/api/portfolios/{plain}", json={"leverage": "5"})
    assert r.status_code == 200, r.text
    r = await admin.client.patch(f"/api/portfolios/{plain}", json={"leverage": "2"})
    assert r.status_code == 200
    b = await _portfolio(admin, link_kind="alpaca_paper", leverage=D(2))
    r = await admin.client.patch(f"/api/portfolios/{b}", json={"auto_rules_enabled": True})
    assert r.status_code == 200, r.text
    _refused(await admin.client.patch(f"/api/portfolios/{b}", json={"leverage": "3"}),
             text=msg)


# --- orders ------------------------------------------------------------------------------


async def test_order_day_trade_from_portal(admin):
    from smtrading.engine.models import Order

    pid = await _portfolio(admin)
    body = {"symbol": "BTC-USD", "side": "buy", "type": "market", "qty": "0.01",
            "day_trade": True}
    r = await admin.client.post(f"/api/portfolios/{pid}/orders/preview", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    r = await admin.client.post(f"/api/portfolios/{pid}/orders", json=body,
                                headers={"Idempotency-Key": "day-1"})
    assert r.status_code == 200, r.text
    assert r.json()["day_trade"] is True
    r = await admin.client.post(f"/api/portfolios/{pid}/orders", json={
        **body, "day_trade": False}, headers={"Idempotency-Key": "day-2"})
    assert r.json()["day_trade"] is False
    listed = (await admin.client.get(f"/api/portfolios/{pid}/orders")).json()
    assert sorted(o["day_trade"] for o in listed) == [False, True]
    async with admin.sm() as s:
        flags = sorted((await s.scalars(select(Order.day_trade).where(
            Order.portfolio_id == uuid.UUID(pid)))).all())
    assert flags == [False, True]


# --- triggers ----------------------------------------------------------------------------


async def _trigger(admin, pid, symbol, at, **kw):
    from smtrading.intraday.models import IntradayTrigger

    async with admin.sm() as s:
        t = IntradayTrigger(
            portfolio_id=uuid.UUID(pid), strategy_ref="STR-9", strategy_name="Prova",
            symbol=symbol, rule="entry", timeframe="15m", bar_close=at, value=D("25.5"),
            threshold=D(30), price=D("101.25"), rule_text="RSI 2 sotto 30",
            created_at=at, **kw)
        s.add(t)
        await s.commit()
        return t.id


async def test_triggers_list_and_delete(admin):
    a = await _portfolio(admin, intraday_enabled=True)
    b = await _portfolio(admin, intraday_enabled=True)
    t0 = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
    first = await _trigger(admin, a, "AAPL", t0, outcome="woken", issue_ident="SMT-12")
    second = await _trigger(admin, a, "MSFT", t0 + timedelta(minutes=15), outcome="failed",
                            error="timeout")
    other = await _trigger(admin, b, "AAPL", t0 + timedelta(minutes=30))

    viewer = await admin.viewer()
    assert (await viewer.get("/api/intraday/triggers")).status_code == 403
    assert (await viewer.delete(f"/api/portfolios/{a}/intraday-triggers")).status_code == 403

    r = await admin.client.get("/api/intraday/triggers")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["id"] for x in rows] == [other, second, first]
    row = rows[2]
    name_a = (await _row(admin, a)).name
    assert row == {
        "id": first, "created_at": row["created_at"], "portfolio_id": a,
        "portfolio_name": name_a, "strategy_ref": "STR-9", "strategy_name": "Prova",
        "symbol": "AAPL", "rule": "entry", "timeframe": "15m",
        "bar_close": row["bar_close"], "value": "25.5", "threshold": "30",
        "price": "101.25", "rule_text": "RSI 2 sotto 30", "outcome": "woken",
        "issue_ident": "SMT-12", "error": None,
    }
    assert datetime.fromisoformat(row["bar_close"]) == t0
    r = await admin.client.get("/api/intraday/triggers", params={"portfolio_id": a, "limit": 1})
    assert [x["id"] for x in r.json()] == [second]
    _refused(await admin.client.get("/api/intraday/triggers", params={"limit": 201}))
    _refused(await admin.client.get("/api/intraday/triggers", params={"limit": 0}))

    r = await admin.client.delete(f"/api/portfolios/{a}/intraday-triggers")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 2}
    assert [x["id"] for x in (await admin.client.get("/api/intraday/triggers")).json()] == [
        other]
    (audit,) = await _audits(admin, "admin:intraday.triggers_delete")
    assert audit.params["portfolio_id"] == a and audit.params["deleted"] == 2
    assert audit.params["by"] == admin.email
    r = await admin.client.delete(f"/api/portfolios/{uuid.uuid4()}/intraday-triggers")
    assert r.status_code == 404


# --- account state -------------------------------------------------------------------------


async def _round_trip(admin, pid, at):
    """A day trade (buy then sell of AAPL) of the portfolio at `at`."""
    from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
    from smtrading.ledger.models import Journal, JournalType

    async with admin.sm() as s:
        for side in (OrderSide.buy, OrderSide.sell):
            o = Order(portfolio_id=uuid.UUID(pid), symbol="AAPL", side=side,
                      type=OrderType.market, qty=D(10), day_trade=side == OrderSide.buy,
                      status=OrderStatus.filled, source="admin")
            s.add(o)
            await s.flush()
            h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                        hash=hashlib.sha256(h.encode()).hexdigest())
            s.add(j)
            await s.flush()
            s.add(Fill(order_id=o.id, portfolio_id=uuid.UUID(pid), symbol="AAPL", side=side,
                       qty=D(10), price=D(100), fee_currency="USD", journal_id=j.id,
                       created_at=at))
        await s.commit()


async def test_accounts_endpoint_shape(admin):
    from smtrading.brokers.models import BrokerLink
    from smtrading.instruments.market_hours import session_on
    from smtrading.intraday.calendar import last_sessions
    from smtrading.portfolios.models import PortfolioStatus

    day = await _portfolio(admin, link_kind="alpaca_paper", intraday_enabled=True,
                           leverage=D(3), max_day_trades_5d=5)
    link = await _link_of(admin, day)
    off = await _portfolio(admin, leverage=D(2))
    real = await _portfolio(admin, link_kind="alpaca_live", auto_rules_enabled=True,
                            leverage=D(2))
    archived = await _portfolio(admin)
    lonely = BrokerLink(name="Senza portafogli", kind="alpaca_paper")
    async with admin.sm() as s:
        s.add(lonely)
        from smtrading.portfolios.models import Portfolio

        (await s.get(Portfolio, uuid.UUID(off))).broker_link_id = link.id
        arch = await s.get(Portfolio, uuid.UUID(archived))
        arch.broker_link_id = link.id
        arch.status = PortfolioStatus.archived
        now = datetime.now(UTC)
        stored = await s.get(BrokerLink, link.id)
        stored.pdt_state = {"buying_power": "60000", "non_marginable_buying_power": "30000",
                            "multiplier": "4", "equity": "30000", "trading_blocked": False,
                            "account_blocked": False, "read_at": now.isoformat()}
        stored.pdt_state_at = now
        await s.commit()
    first_day = last_sessions("NYSE", datetime.now(UTC), 5)[0]
    await _round_trip(admin, day, session_on("NYSE", first_day)[0] + timedelta(minutes=30))

    viewer = await admin.viewer()
    assert (await viewer.get("/api/intraday/accounts")).status_code == 403
    assert (await admin.client.get("/api/intraday/pdt")).status_code == 404
    r = await admin.client.get("/api/intraday/accounts")
    assert r.status_code == 200, r.text
    assert ACCT_ID not in r.text and KEY not in r.text and "account_id" not in r.text
    rows = {x["link_name"]: x for x in r.json()}
    real_link = await _link_of(admin, real)
    assert set(rows) == {link.name, real_link.name}
    link_keys = {"link_id", "link_name", "kind", "buying_power", "multiplier", "equity",
                 "trading_blocked", "account_blocked", "read_at", "fresh", "margin",
                 "portfolios"}
    pf_keys = {"portfolio_id", "portfolio_name", "intraday_enabled", "auto_rules_enabled",
               "leverage", "day_leverage", "max_day_trades_5d", "used", "pending",
               "remaining", "frees_on"}
    paper = rows[link.name]
    assert set(paper) == link_keys
    assert (paper["link_id"], paper["kind"]) == (str(link.id), "alpaca_paper")
    assert (paper["buying_power"], paper["multiplier"], paper["equity"]) == (
        "60000", "4", "30000")
    assert paper["trading_blocked"] is False and paper["account_blocked"] is False
    assert paper["fresh"] is True and paper["margin"] is True and paper["read_at"]
    by_pf = {x["portfolio_id"]: x for x in paper["portfolios"]}
    assert set(by_pf) == {day, off}
    assert all(set(x) == pf_keys for x in paper["portfolios"])
    d = by_pf[day]
    assert d["intraday_enabled"] is True and d["auto_rules_enabled"] is False
    assert (d["leverage"], d["day_leverage"], d["max_day_trades_5d"]) == ("3", "2", 5)
    assert d["used"] == 1 and d["pending"] == 0 and d["remaining"] == 4
    assert d["frees_on"] is not None
    o = by_pf[off]
    assert (o["used"], o["pending"], o["remaining"], o["frees_on"]) == (None,) * 4
    assert o["day_leverage"] == "2"

    live = rows[real_link.name]
    assert set(live) == link_keys and live["kind"] == "alpaca_live"
    assert live["fresh"] is False and live["margin"] is False
    assert live["buying_power"] is None and live["read_at"] is None
    (rp,) = live["portfolios"]
    assert rp["day_leverage"] == "1" and rp["used"] == 0 and rp["remaining"] is None


# --- open positions ------------------------------------------------------------------------


async def test_open_positions_day_qty(db_session):
    from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.ledger.models import Account, AccountBalance, AccountKind, Journal, JournalType
    from smtrading.portfolios.service import create_portfolio
    from smtrading.positions.models import Position
    from smtrading.positions.open_view import open_positions
    from smtrading.seed import run_seed

    await run_seed(db_session)
    wed = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # NYSE open
    on = await create_portfolio(db_session, name="Giornata", initial_cash=D(100000))
    on.intraday_enabled = True
    off = await create_portfolio(db_session, name="Campagna", initial_cash=D(100000))
    await db_session.flush()
    for p in (on, off):
        acct = Account(portfolio_id=p.id, kind=AccountKind.position, asset="AAPL")
        db_session.add(acct)
        await db_session.flush()
        db_session.add(AccountBalance(account_id=acct.id, balance=D(15)))
        db_session.add(Position(portfolio_id=p.id, symbol="AAPL", qty=D(15),
                                avg_price=D(100)))
        o = Order(portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy,
                  type=OrderType.market, qty=D(10), day_trade=True,
                  status=OrderStatus.filled, source="admin")
        db_session.add(o)
        await db_session.flush()
        h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                    hash=hashlib.sha256(h.encode()).hexdigest())
        db_session.add(j)
        await db_session.flush()
        db_session.add(Fill(order_id=o.id, portfolio_id=p.id, symbol="AAPL",
                            side=OrderSide.buy, qty=D(10), price=D(100), fee_currency="USD",
                            journal_id=j.id, created_at=wed - timedelta(hours=1)))
    await db_session.flush()
    qs = FakeMarketData()
    qs.set_fx("USD", "EUR", D("0.8"))
    qs.set_quote("AAPL", D("99.90"), D("100.10"), D("100.00"))
    out = await open_positions(db_session, qs, wed)
    rows = {r["portfolio_name"]: r for r in out["positions"]}
    assert rows["Giornata"]["day_qty"] == D(10)
    assert rows["Campagna"]["day_qty"] is None
    out = await open_positions(db_session, qs, wed + timedelta(days=1))  # next day: none
    assert {r["portfolio_name"]: r["day_qty"] for r in out["positions"]} == {
        "Giornata": D(0), "Campagna": None}


# --- risk profile --------------------------------------------------------------------------


async def test_risk_profile_margin_bounds(admin):
    r = await admin.client.post("/api/risk-profiles", json={"name": "Margine"})
    assert r.status_code == 200, r.text
    profile = r.json()
    assert profile["day_close_limit_margin_pct"] == "0.5"
    for bad in ("0", "6", "-1", None):
        r = await admin.client.patch(f"/api/risk-profiles/{profile['id']}",
                                     json={"day_close_limit_margin_pct": bad})
        _refused(r)
    r = await admin.client.patch(f"/api/risk-profiles/{profile['id']}",
                                 json={"day_close_limit_margin_pct": "0.75"})
    assert r.status_code == 200, r.text
    assert r.json()["day_close_limit_margin_pct"] == "0.75"
    r = await admin.client.patch(f"/api/risk-profiles/{profile['id']}",
                                 json={"day_close_limit_margin_pct": "5"})
    assert r.status_code == 200 and r.json()["day_close_limit_margin_pct"] == "5"
    listed = {p["id"]: p for p in (await admin.client.get("/api/risk-profiles")).json()}
    assert listed[profile["id"]]["day_close_limit_margin_pct"] == "5"
    r = await admin.client.patch(f"/api/risk-profiles/{profile['id']}",
                                 json={"description": "solo testo"})
    assert r.status_code == 200 and r.json()["day_close_limit_margin_pct"] == "5"


# --- drill CLI -----------------------------------------------------------------------------


async def _cli(sm, redis_url, *argv):
    from redis.asyncio import Redis

    from smtrading import cli
    from smtrading.engine.quotesource import FakeQuoteSource

    redis = Redis.from_url(redis_url)
    try:
        code, out = await cli.run_command(cli.build_parser().parse_args(list(argv)), sm,
                                          FakeQuoteSource(), redis)
    finally:
        await redis.aclose()
    return code, json.loads(out)


async def _decisions(sm, pid=None):
    from smtrading.activity.models import Decision

    async with sm() as s:
        q = select(Decision).order_by(Decision.id)
        if pid is not None:
            q = q.where(Decision.portfolio_id == uuid.UUID(pid))
        return list(await s.scalars(q))


async def _ceo(sm, name="CEO"):
    from smtrading.agents import service as agents

    async with sm() as s:
        agent, _ = await agents.create_agent(s, name)
        await s.commit()
        return agent


def _drill_args(pid, agent="CEO", ref="DRILL-1"):
    return ("intraday", "drill-strategy", "--portfolio", pid, "--agent", agent,
            "--ref", ref, "--type", "rsi_reversion",
            "--params", '{"period": 2, "buy_below": 10, "sell_above": 70}',
            "--timeframe", "15m", "--symbols", "aapl,MSFT")


async def test_drill_strategy_refuses_agent_portfolio(admin):
    await _ceo(admin.sm)
    with_agent = await _portfolio(admin, intraday_enabled=True)
    await _agent(admin, with_agent, "read")
    code, out = await _cli(admin.sm, admin.redis_url, *_drill_args(with_agent))
    assert code == 1 and out["error"]["code"] == "INVALID_ORDER", out
    code, out = await _cli(admin.sm, admin.redis_url, "intraday", "drill-revoke",
                           "--portfolio", with_agent, "--agent", "CEO", "--ref", "DRILL-1",
                           "--purge")
    assert code == 1, out
    switched_off = await _portfolio(admin)
    code, out = await _cli(admin.sm, admin.redis_url, *_drill_args(switched_off))
    assert code == 1 and out["error"]["code"] == "INVALID_ORDER", out
    hidden = await _portfolio(admin, intraday_enabled=True)
    for bad in (("--timeframe", "1d"), ("--type", "unknown"), ("--params", "[1")):
        args = list(_drill_args(hidden))
        i = args.index(bad[0])
        args[i + 1] = bad[1]
        code, out = await _cli(admin.sm, admin.redis_url, *args)
        assert code == 1 and out["error"]["code"] == "INVALID_ORDER", (bad, out)
    assert await _decisions(admin.sm) == []


async def test_drill_strategy_is_invisible_to_agents(mcp_server, mcp_call):
    from smtrading.portfolios.service import create_portfolio
    from smtrading.seed import run_seed
    from smtrading.signals.service import approved_strategies

    engine = make_engine(mcp_server.db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            await run_seed(s)
            p = await create_portfolio(s, name="Prova giornata", initial_cash=D(30000))
            p.intraday_enabled = True
            await s.commit()
        await _ceo(sm, "CEO")
        code, out = await _cli(sm, get_settings().redis_url, *_drill_args(str(p.id)))
        assert code == 0, out
        (d,) = await _decisions(sm)
        assert d.kind == "strategy" and d.portfolio_id == p.id
        assert d.summary == "Esercitazione lavoro in giornata"
        assert d.symbols == ["AAPL", "MSFT"]
        assert d.details == {"ref": "DRILL-1", "status": "approvata",
                             "strategy": "rsi_reversion",
                             "params": {"period": 2, "buy_below": 10, "sell_above": 70},
                             "timeframe": "15m"}
        assert out["decision_id"] == d.id

        err, res = await mcp_call(mcp_server.url, mcp_server.token, "get_decisions", {})
        assert not err, res
        assert res["decisions"] == []
        err, res = await mcp_call(mcp_server.url, mcp_server.token, "get_activity_feed", {})
        assert not err, res
        assert "DRILL-1" not in json.dumps(res)
        async with sm() as s:
            [a] = [x for x in await approved_strategies(s) if x.ref == "DRILL-1"]
            assert [x.id for x in a.portfolios] == [p.id] and a.timeframe == "15m"
    finally:
        await engine.dispose()


async def test_drill_revoke_purge_deletes_decisions(admin):
    from smtrading.signals.service import approved_strategies

    await _ceo(admin.sm)
    hidden = await _portfolio(admin, intraday_enabled=True)
    other = await _portfolio(admin)
    code, out = await _cli(admin.sm, admin.redis_url, *_drill_args(hidden))
    assert code == 0, out
    code, out = await _cli(admin.sm, admin.redis_url, "intraday", "drill-revoke",
                           "--portfolio", hidden, "--agent", "CEO", "--ref", "DRILL-1")
    assert code == 0, out
    rows = await _decisions(admin.sm, hidden)
    assert [r.details["status"] for r in rows] == ["approvata", "revocata"]
    assert rows[1].details["ref"] == "DRILL-1" and rows[1].kind == "strategy"
    async with admin.sm() as s:
        assert [a for a in await approved_strategies(s) if a.ref == "DRILL-1"] == []
    from smtrading.activity.models import Decision

    ceo_id = rows[0].agent_id
    async with admin.sm() as s:  # a decision of another portfolio stays
        s.add(Decision(agent_id=ceo_id, kind="other", summary="altro",
                       portfolio_id=uuid.UUID(other)))
        await s.commit()
    code, out = await _cli(admin.sm, admin.redis_url, "intraday", "drill-revoke",
                           "--portfolio", hidden, "--agent", "CEO", "--ref", "DRILL-1",
                           "--purge")
    assert code == 0 and out == {"deleted": 2}, out
    assert await _decisions(admin.sm, hidden) == []
    assert len(await _decisions(admin.sm, other)) == 1
    async with admin.sm() as s:
        from smtrading.activity.models import Decision as Dc

        assert await s.scalar(select(func.count()).select_from(Dc)) == 1


async def test_drill_refuses_a_ref_used_outside_the_drill_portfolio(admin):
    """Strategy state is keyed by ref alone: a drill on an agent's ref (or a global one,
    or one written as strategy_id with blanks) is refused and changes nothing."""
    from smtrading.activity.models import Decision
    from smtrading.signals.service import approved_strategies

    ceo = await _ceo(admin.sm)
    team = await _portfolio(admin)
    await _agent(admin, team, "trade")
    hidden = await _portfolio(admin, intraday_enabled=True)
    async with admin.sm() as s:
        for pid, det in ((uuid.UUID(team), {"ref": "STR-007"}),
                         (None, {"ref": "STR-GLOB"}),
                         (uuid.UUID(team), {"strategy_id": " STR-ID "})):
            s.add(Decision(agent_id=ceo.id, kind="strategy", summary="Squadra",
                           portfolio_id=pid, symbols=["AAPL"], details={
                               **det, "status": "approvata", "timeframe": "1d",
                               "strategy": "rsi_reversion",
                               "params": {"period": 2, "buy_below": 10, "sell_above": 70}}))
        await s.commit()

    async def approved():
        async with admin.sm() as s:
            return {(a.ref, tuple(p.id for p in a.portfolios), a.timeframe)
                    for a in await approved_strategies(s)}

    before = await approved()
    assert {r for r, _, _ in before} == {"STR-007", "STR-GLOB", "STR-ID"}
    for ref in ("STR-007", " STR-007 ", "STR-GLOB", "STR-ID"):
        code, out = await _cli(admin.sm, admin.redis_url, *_drill_args(hidden, ref=ref))
        assert code == 1 and out["error"]["code"] == "INVALID_ORDER", (ref, out)
        assert "Riferimento già usato" in out["error"]["message"]
        code, out = await _cli(admin.sm, admin.redis_url, "intraday", "drill-revoke",
                               "--portfolio", hidden, "--agent", "CEO", "--ref", ref)
        assert code == 1 and out["error"]["code"] == "INVALID_ORDER", (ref, out)
    assert await approved() == before
    assert await _decisions(admin.sm, hidden) == []
    # a fresh ref works, and its own revoke too (the ref lives only on the drill portfolio)
    code, out = await _cli(admin.sm, admin.redis_url, *_drill_args(hidden, ref="DRILL-9"))
    assert code == 0, out
    code, out = await _cli(admin.sm, admin.redis_url, "intraday", "drill-revoke",
                           "--portfolio", hidden, "--agent", "CEO", "--ref", "DRILL-9")
    assert code == 0, out
    assert await approved() == before
