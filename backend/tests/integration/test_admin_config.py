"""Admin API: agents + audit, fee rules + simulator, risk profiles, interest rates,
app settings, kill switch."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal


PASSWORD = "correct-horse-battery"


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    """A logged-in client (CSRF header set) on a seeded fresh database.

    Yields a namespace with `client`, `sm` (sessionmaker), `qs` (the FakeMarketData of
    the app runtime) and `redis_url`."""
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
    qs.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
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
            ns.client, ns.sm, ns.qs, ns.redis_url = client, sm, qs, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


async def _portfolio(client, **extra) -> dict:
    r = await client.post(
        "/api/portfolios", json={"name": f"P-{uuid.uuid4().hex[:8]}", "initial_cash": "10000", **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _agent(client, name=None) -> dict:
    r = await client.post("/api/agents", json={"name": name or f"a-{uuid.uuid4().hex[:8]}"})
    assert r.status_code == 200, r.text
    return r.json()


# --- agents --------------------------------------------------------------------------


async def test_agent_token_only_on_create_and_rotate(admin):
    c = admin.client
    created = await _agent(c, "bot-1")
    token = created["token"]
    assert token and created["agent"]["name"] == "bot-1" and created["agent"]["active"] is True
    assert created["agent"]["portfolios"] == [] and "token" not in created["agent"]

    listed = await c.get("/api/agents")
    assert token not in listed.text and "token_hash" not in listed.text
    assert all("token" not in a for a in listed.json())
    mine = next(a for a in listed.json() if a["name"] == "bot-1")
    assert mine["last_seen_at"] is None
    assert "Demo" in {p["portfolio_name"] for a in listed.json() for p in a["portfolios"]}
    demo = next(p for a in listed.json() for p in a["portfolios"] if p["portfolio_name"] == "Demo")
    assert demo["archived"] is False and demo["mode"] == "simulazione"

    assert (await c.post("/api/agents", json={"name": "bot-1"})).json()["details"]["field"] == "name"
    assert (await c.post("/api/agents", json={"name": "  "})).status_code == 400
    assert (await c.post(f"/api/agents/{uuid.uuid4()}/rotate")).status_code == 404

    rotated = await c.post(f"/api/agents/{created['agent']['id']}/rotate")
    assert rotated.status_code == 200 and set(rotated.json()) == {"token"}
    assert rotated.json()["token"] != token
    assert rotated.json()["token"] not in (await c.get("/api/agents")).text


async def test_rotated_token_stops_working_at_once(admin):
    from smtrading.agents import service as agents

    c = admin.client
    created = await _agent(c)
    old = created["token"]
    async with admin.sm() as s:
        assert (await agents.authenticate(s, old)).id == uuid.UUID(created["agent"]["id"])  # cached
    new = (await c.post(f"/api/agents/{created['agent']['id']}/rotate")).json()["token"]
    async with admin.sm() as s:
        with pytest.raises(Exception, match="invalid token"):
            await agents.authenticate(s, old)
        assert (await agents.authenticate(s, new)).name == created["agent"]["name"]

    patched = await c.patch(f"/api/agents/{created['agent']['id']}", json={"active": False})
    assert patched.json()["active"] is False
    async with admin.sm() as s:
        with pytest.raises(Exception, match="invalid token"):
            await agents.authenticate(s, new)


async def test_agent_portfolios_replace_whole_set(admin):
    c = admin.client
    agent = (await _agent(c))["agent"]
    p1, p2, p3 = [(await _portfolio(c))["id"] for _ in range(3)]
    url = f"/api/agents/{agent['id']}/portfolios"

    r = await c.put(url, json=[
        {"portfolio_id": p1, "permission": "trade"}, {"portfolio_id": p2, "permission": "read"},
    ])
    assert r.status_code == 200, r.text
    assert {(p["portfolio_id"], p["permission"]) for p in r.json()["portfolios"]} == {
        (p1, "trade"), (p2, "read"),
    }
    r = await c.put(url, json=[{"portfolio_id": p3, "permission": "read"}])
    assert [(p["portfolio_id"], p["permission"]) for p in r.json()["portfolios"]] == [(p3, "read")]
    listed = next(a for a in (await c.get("/api/agents")).json() if a["id"] == agent["id"])
    assert [p["portfolio_id"] for p in listed["portfolios"]] == [p3]

    # invalid input leaves the previous set untouched
    for bad in (
        [{"portfolio_id": p1, "permission": "admin"}],
        [{"portfolio_id": p1, "permission": "read"}, {"portfolio_id": p1, "permission": "trade"}],
    ):
        assert (await c.put(url, json=bad)).status_code == 400
    missing = await c.put(url, json=[{"portfolio_id": str(uuid.uuid4()), "permission": "read"}])
    assert missing.status_code == 404
    listed = next(a for a in (await c.get("/api/agents")).json() if a["id"] == agent["id"])
    assert [p["portfolio_id"] for p in listed["portfolios"]] == [p3]

    assert (await c.put(url, json=[])).json()["portfolios"] == []
    assert (await c.put(f"/api/agents/{uuid.uuid4()}/portfolios", json=[])).status_code == 404


async def test_mcp_audit_filters(admin):
    from smtrading.agents import service as agents

    c = admin.client
    a1, a2 = (await _agent(c))["agent"], (await _agent(c))["agent"]
    async with admin.sm() as s:
        for agent, method, outcome in (
            (a1, "get_portfolio", "ok"), (a1, "place_order", "INVALID_ORDER"),
            (a2, "place_order", "ok"),
        ):
            await agents.write_audit(
                s, agent_id=uuid.UUID(agent["id"]), method=method,
                params={"qty": D("1.5")}, outcome=outcome, latency_ms=12,
            )
        await s.commit()

    async def audit(**q):
        r = await c.get("/api/mcp-audit", params=q)
        assert r.status_code == 200, r.text
        return r.json()

    assert len(await audit(agent_id=a1["id"])) == 2
    rows = await audit(agent_id=a1["id"], method="place_order")
    assert len(rows) == 1 and rows[0]["outcome"] == "INVALID_ORDER"
    assert rows[0]["params"] == {"qty": "1.5"} and rows[0]["latency_ms"] == 12
    assert len(await audit(method="place_order", outcome="ok")) == 1
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert await audit(since=future) == []
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert len(await audit(since=past, agent_id=a2["id"])) == 1
    assert len(await audit(limit=2)) == 2
    assert (await c.get("/api/mcp-audit", params={"limit": 0})).status_code == 400
    assert (await c.get("/api/mcp-audit", params={"since": "ieri"})).status_code == 400


# --- fee rules -----------------------------------------------------------------------


async def test_fee_rule_crud_and_deactivate(admin):
    c = admin.client
    base = (await c.get("/api/fee-rules")).json()
    assert [r["scope"] for r in base] == ["global"]

    body = {
        "name": "Crypto", "scope": "asset_class", "scope_ref": "crypto", "side": "buy",
        "fixed": "0.5", "pct": "0.2", "min_fee": "1", "max_fee": "30", "fx_pct": "0.1",
        "priority": 3,
    }
    r = await c.post("/api/fee-rules", json=body)
    assert r.status_code == 200, r.text
    rule = r.json()
    assert (rule["fixed"], rule["pct"], rule["min_fee"], rule["max_fee"]) == ("0.5", "0.2", "1", "30")
    assert rule["active"] is True and rule["tiers"] is None and rule["currency"] == "EUR"

    dup = await c.post("/api/fee-rules", json=body)
    assert dup.status_code == 400 and dup.json()["details"]["field"] == "name"
    # the same name under another scope_ref is fine
    ok = await c.post("/api/fee-rules", json={**body, "scope_ref": "forex"})
    assert ok.status_code == 200

    r = await c.patch(f"/api/fee-rules/{rule['id']}", json={"pct": "0.3", "max_fee": None})
    assert r.status_code == 200, r.text
    assert (r.json()["pct"], r.json()["max_fee"], r.json()["min_fee"]) == ("0.3", None, "1")
    assert (await c.patch(f"/api/fee-rules/{rule['id']}", json={"min_fee": "5", "max_fee": "2"})
            ).status_code == 400
    assert (await c.patch(f"/api/fee-rules/{rule['id']}", json={"name": None})).status_code == 400
    assert (await c.patch(f"/api/fee-rules/{uuid.uuid4()}", json={"pct": "1"})).status_code == 404

    r = await c.delete(f"/api/fee-rules/{rule['id']}")
    assert r.status_code == 200 and r.json()["active"] is False
    again = {x["id"]: x for x in (await c.get("/api/fee-rules")).json()}
    assert again[rule["id"]]["active"] is False and len(again) == 3
    assert (await c.delete(f"/api/fee-rules/{uuid.uuid4()}")).status_code == 404


async def test_last_global_fee_rule_cannot_be_deactivated(admin):
    c = admin.client
    glob = (await c.get("/api/fee-rules")).json()[0]
    msg = "Deve restare una regola globale attiva per entrambi i lati"
    r = await c.delete(f"/api/fee-rules/{glob['id']}")
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER" and r.json()["message"] == msg
    for patch in ({"active": False}, {"side": "buy"}, {"scope": "asset_class", "scope_ref": "crypto"}):
        r = await c.patch(f"/api/fee-rules/{glob['id']}", json=patch)
        assert r.status_code == 400 and r.json()["message"] == msg, patch
    assert next(x for x in (await c.get("/api/fee-rules")).json() if x["id"] == glob["id"])[
        "active"
    ] is True

    # with a second global rule for both sides the first can go
    other = await c.post("/api/fee-rules", json={"name": "Altra", "pct": "0.2"})
    assert other.status_code == 200
    assert (await c.delete(f"/api/fee-rules/{glob['id']}")).json()["active"] is False
    assert (await c.delete(f"/api/fee-rules/{other.json()['id']}")).status_code == 400


async def test_fee_rule_tiers_validation_and_storage(admin):
    c = admin.client
    pid = (await _portfolio(c))["id"]
    rule = {"name": "T", "scope": "portfolio", "scope_ref": pid}
    good = [{"up_to": 1000, "pct": "0.5"}, {"up_to": "5000", "pct": "0.3"}, {"up_to": None, "pct": "0.1"}]
    r = await c.post("/api/fee-rules", json={**rule, "tiers": good})
    assert r.status_code == 200, r.text
    assert r.json()["tiers"] == [
        {"up_to": "1000", "pct": "0.5"}, {"up_to": "5000", "pct": "0.3"},
        {"up_to": None, "pct": "0.1"},
    ]
    from smtrading.fees.models import FeeRule

    async with admin.sm() as s:
        stored = (await s.get(FeeRule, uuid.UUID(r.json()["id"]))).tiers
    assert all(isinstance(t["pct"], str) for t in stored)

    bad_tiers = {
        "descending": [{"up_to": "5000", "pct": "0.5"}, {"up_to": "1000", "pct": "0.3"},
                       {"up_to": None, "pct": "0.1"}],
        "equal": [{"up_to": "1000", "pct": "0.5"}, {"up_to": "1000", "pct": "0.3"},
                  {"up_to": None, "pct": "0.1"}],
        "last not null": [{"up_to": "1000", "pct": "0.5"}, {"up_to": "5000", "pct": "0.3"}],
        "null in the middle": [{"up_to": None, "pct": "0.5"}, {"up_to": None, "pct": "0.1"}],
        "negative pct": [{"up_to": "1000", "pct": "-1"}, {"up_to": None, "pct": "0.1"}],
    }
    for label, tiers in bad_tiers.items():
        r = await c.post("/api/fee-rules", json={**rule, "name": label, "tiers": tiers})
        assert r.status_code == 400, label
        assert r.json()["code"] == "INVALID_ORDER" and r.json()["details"]["field"] == "tiers", label


async def test_fee_rule_field_validation(admin):
    c = admin.client
    base = {"name": "X"}
    cases = [
        ({"pct": "-1"}, "pct"),
        ({"fixed": "-0.1"}, "fixed"),
        ({"fx_pct": "-1"}, "fx_pct"),
        ({"min_fee": "5", "max_fee": "1"}, "min_fee"),
        ({"scope": "galaxy"}, "scope"),
        ({"side": "up"}, "side"),
        ({"scope": "global", "scope_ref": "crypto"}, "scope_ref"),
        ({"scope": "asset_class"}, "scope_ref"),
        ({"scope": "asset_class", "scope_ref": "bonds"}, "scope_ref"),
        ({"scope": "portfolio", "scope_ref": "not-a-uuid"}, "scope_ref"),
        ({"scope": "portfolio", "scope_ref": str(uuid.uuid4())}, "scope_ref"),
        ({"currency": "euro!"}, "currency"),
        ({"name": " "}, "name"),
        ({"pct": "abc"}, "pct"),
    ]
    for extra, field in cases:
        r = await c.post("/api/fee-rules", json={**base, **extra})
        assert r.status_code == 400, extra
        assert r.json()["details"]["field"] == field, (extra, r.json())


async def test_fee_simulator_uses_portfolio_rule(admin):
    c = admin.client
    pid = (await _portfolio(c))["id"]
    sim = {"portfolio_id": pid, "symbol": "AAPL", "side": "buy", "qty": "10"}

    # no portfolio rule yet: the seeded global one (1 + 0.1%, 1..20, fx 0.25%) applies
    r = await c.post("/api/fee-rules/simulate", json=sim)
    assert r.status_code == 200, r.text
    glob = r.json()
    assert glob["rule_name"] == "Default globale" and glob["fee_currency"] == "EUR"
    assert glob["notional_eur"] == "1600.8"  # 10 x 200.10 USD at 1.25
    assert glob["fee"] == "2.6"
    assert D(glob["fx_fee_eur"]) == D("4.002").quantize(D("0.01"))

    made = await c.post("/api/fee-rules", json={
        "name": "Mine", "scope": "portfolio", "scope_ref": pid, "fixed": "3", "pct": "0.5",
        "fx_pct": "1",
    })
    assert made.status_code == 200, made.text
    r = await c.post("/api/fee-rules/simulate", json=sim)
    mine = r.json()
    assert (mine["rule_id"], mine["rule_name"]) == (made.json()["id"], "Mine")
    assert mine["fee"] == "11" and D(mine["fx_fee_eur"]) == D("16.01")
    assert mine["notional_eur"] == "1600.8"

    # by notional (EUR), another side; a deactivated rule falls back to the global one
    r = await c.post("/api/fee-rules/simulate", json={**sim, "qty": None, "notional": "1000"})
    assert r.status_code == 200 and r.json()["rule_name"] == "Mine"
    await c.delete(f"/api/fee-rules/{made.json()['id']}")
    r = await c.post("/api/fee-rules/simulate", json=sim)
    assert r.json()["rule_name"] == "Default globale"

    for bad in (
        {**sim, "qty": None},
        {**sim, "notional": "100"},
        {**sim, "side": "hold"},
        {**sim, "symbol": "NOPE"},
    ):
        assert (await c.post("/api/fee-rules/simulate", json=bad)).status_code == 400, bad
    assert (await c.post("/api/fee-rules/simulate",
                         json={**sim, "portfolio_id": str(uuid.uuid4())})).status_code == 404


async def test_fees_simulate_by_class_priority_and_portfolio_override(admin):
    """POST /api/fees/simulate: which rule wins (portfolio > class > global, then priority)
    and the amounts for a notional in the instrument currency."""
    c = admin.client
    pid = (await _portfolio(c))["id"]
    sim = {"portfolio_id": pid, "asset_class": "us_equity", "side": "buy",
           "notional": "1000", "currency": "EUR"}
    r = await c.post("/api/fees/simulate", json=sim)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rule_name"] == "Default globale"
    assert out["fee"] == "2" and out["currency"] == "EUR"  # 1 + 0.1% x 1000
    assert out["fx_fee"] == "0"  # same currency as the portfolio: no conversion

    usd = (await c.post("/api/fees/simulate", json={**sim, "currency": "USD"})).json()
    assert D(usd["fx_fee"]) == D("2")  # 0.25% of 800 EUR (1000 USD at 1.25)
    assert usd["fx_currency"] == "EUR"

    low = {"scope": "asset_class", "scope_ref": "us_equity", "fixed": "0", "pct": "0",
           "fx_pct": "0"}
    await c.post("/api/fee-rules", json={**low, "name": "Classe A", "priority": 1})
    await c.post("/api/fee-rules", json={**low, "name": "Classe B", "priority": 5,
                                         "fixed": "4"})
    r = await c.post("/api/fees/simulate", json=sim)
    assert r.json()["rule_name"] == "Classe B" and r.json()["fee"] == "4"
    crypto = (await c.post("/api/fees/simulate", json={**sim, "asset_class": "crypto"})).json()
    assert crypto["rule_name"] == "Default globale"

    await c.post("/api/fee-rules", json={"name": "Solo questo", "scope": "portfolio",
                                         "scope_ref": pid, "fixed": "0.5", "pct": "0"})
    r = await c.post("/api/fees/simulate", json=sim)
    assert r.json()["rule_name"] == "Solo questo" and r.json()["fee"] == "0.5"

    for bad in ({**sim, "notional": "-1"}, {**sim, "asset_class": "bonds"},
                {**sim, "side": "hold"}, {**sim, "currency": "euro!"}):
        assert (await c.post("/api/fees/simulate", json=bad)).status_code == 400, bad
    r = await c.post("/api/fees/simulate", json={**sim, "portfolio_id": str(uuid.uuid4())})
    assert r.status_code == 404


# --- broker fee presets ------------------------------------------------------------------


async def test_broker_fee_presets_list(admin):
    r = await admin.client.get("/api/brokers/fee-presets")
    assert r.status_code == 200, r.text
    rows = {p["broker_kind"]: p for p in r.json()}
    assert set(rows) == {"alpaca_paper", "alpaca_live", "t212_demo", "t212_live",
                         "etoro_demo", "etoro_live"}
    assert (rows["alpaca_live"]["pct"], rows["alpaca_live"]["fx_pct"]) == ("0", "0")
    assert rows["t212_demo"]["fx_pct"] == "0.15" and rows["etoro_live"]["fx_pct"] == "0.5"
    assert all(p["source_note"] == "listino pubblico, da verificare" for p in rows.values())


async def test_apply_broker_fee_preset(admin):
    """The rule «<portafoglio> – <broker>» is created (then updated, never duplicated); the
    portfolio's other active portfolio-scope rules are deactivated, never deleted."""
    c = admin.client
    pf = await _portfolio(c)
    pid, name = pf["id"], pf["name"]
    old = (await c.post("/api/fee-rules", json={
        "name": f"{name} – zero commissioni", "scope": "portfolio", "scope_ref": pid,
    })).json()
    other_pf = await _portfolio(c)
    untouched = (await c.post("/api/fee-rules", json={
        "name": "Altro portafoglio", "scope": "portfolio", "scope_ref": other_pf["id"],
    })).json()
    url = f"/api/portfolios/{pid}/fee-rule/apply-preset"

    r = await c.post(url, json={"broker_kind": "t212_demo"})
    assert r.status_code == 200, r.text
    out = r.json()
    rule = out["rule"]
    assert out["created"] is True
    assert (rule["name"], rule["scope"], rule["scope_ref"]) == (
        f"{name} – Trading 212", "portfolio", pid)
    assert (rule["side"], rule["pct"], rule["fixed"], rule["fx_pct"], rule["active"]) == (
        "both", "0", "0", "0.15", True)
    assert rule["currency"] == pf["base_currency"]
    assert out["deactivated"] == [{"id": old["id"], "name": old["name"]}]
    assert out["preset"]["broker"] == "Trading 212"

    rules = {x["id"]: x for x in (await c.get("/api/fee-rules")).json()}
    assert rules[old["id"]]["active"] is False
    assert rules[untouched["id"]]["active"] is True
    sim = {"portfolio_id": pid, "asset_class": "us_equity", "side": "sell",
           "notional": "1000", "currency": "USD"}
    sim_out = (await c.post("/api/fees/simulate", json=sim)).json()
    assert sim_out["rule_name"] == rule["name"] and sim_out["fee"] == "0"
    assert D(sim_out["fx_fee"]) == D("1.2")  # 0.15% of 800 EUR

    # applied again: the same rule, updated (a manual edit is overwritten)
    await c.patch(f"/api/fee-rules/{rule['id']}", json={"fx_pct": "3", "active": False})
    again = (await c.post(url, json={"broker_kind": "t212_live"})).json()
    assert again["created"] is False and again["rule"]["id"] == rule["id"]
    assert (again["rule"]["fx_pct"], again["rule"]["active"]) == ("0.15", True)
    assert again["deactivated"] == []

    # another broker: its own rule, the Trading 212 one deactivated
    etoro = (await c.post(url, json={"broker_kind": "etoro_live"})).json()
    assert etoro["rule"]["name"] == f"{name} – eToro" and etoro["rule"]["fx_pct"] == "0.5"
    assert etoro["deactivated"] == [{"id": rule["id"], "name": rule["name"]}]
    names = [x["name"] for x in (await c.get("/api/fee-rules")).json()
             if x["scope_ref"] == pid]
    assert sorted(names) == sorted([old["name"], rule["name"], etoro["rule"]["name"]])


async def test_apply_broker_fee_preset_refusals(admin):
    c = admin.client
    pid = (await _portfolio(c))["id"]
    url = f"/api/portfolios/{pid}/fee-rule/apply-preset"
    r = await c.post(url, json={"broker_kind": "ibkr_live"})
    assert r.status_code == 400 and r.json()["details"]["field"] == "broker_kind"
    assert (await c.post(url, json={})).status_code in (400, 422)
    r = await c.post(f"/api/portfolios/{uuid.uuid4()}/fee-rule/apply-preset",
                     json={"broker_kind": "alpaca_paper"})
    assert r.status_code == 404
    before = len((await c.get("/api/fee-rules")).json())
    assert before == 1  # nothing created by the refusals (only the seeded global rule)


async def test_risk_profiles_count_active_portfolios(admin):
    c = admin.client
    profiles = (await c.get("/api/risk-profiles")).json()
    default = next(p for p in profiles if p["name"] == "Default")
    base = default["portfolios_count"]
    pid = (await _portfolio(c))["id"]
    await c.patch(f"/api/portfolios/{pid}", json={"risk_profile_id": default["id"]})
    counts = {p["name"]: p["portfolios_count"] for p in
              (await c.get("/api/risk-profiles")).json()}
    assert counts["Default"] == base + 1 and counts["5 · Passivo ETF"] == 0
    assert (await c.post(f"/api/portfolios/{pid}/archive")).status_code == 200
    counts = {p["name"]: p["portfolios_count"] for p in
              (await c.get("/api/risk-profiles")).json()}
    assert counts["Default"] == base  # archived portfolios are not counted


# --- risk ----------------------------------------------------------------------------


async def test_risk_profiles_crud(admin):
    c = admin.client
    base = (await c.get("/api/risk-profiles")).json()
    default = next(p for p in base if p["name"] == "Default")
    assert default["max_daily_loss_pct"] == "10" and default["description"].startswith("Libero")
    assert len(base) == 8 and all(p["description"] for p in base)

    r = await c.post("/api/risk-profiles", json={
        "name": "Prudente", "max_order_notional_eur": "5000", "max_position_pct": 25,
        "allowed_asset_classes": ["us_equity", "crypto", "us_equity"],
        "allowed_symbols": ["AAPL"], "short_allowed": False,
    })
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["max_order_notional_eur"], p["max_position_pct"]) == ("5000", "25")
    assert p["allowed_asset_classes"] == ["us_equity", "crypto"] and p["short_allowed"] is False
    assert p["max_daily_loss_eur"] is None and p["allowed_markets"] == []
    assert p["description"] is None

    dup = await c.post("/api/risk-profiles", json={"name": "Prudente"})
    assert dup.status_code == 400 and dup.json()["details"]["field"] == "name"

    r = await c.patch(f"/api/risk-profiles/{p['id']}", json={
        "max_daily_loss_eur": "250", "max_order_notional_eur": None, "short_allowed": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["max_daily_loss_eur"] == "250" and r.json()["max_order_notional_eur"] is None
    assert r.json()["max_position_pct"] == "25" and r.json()["short_allowed"] is True
    assert (await c.patch(f"/api/risk-profiles/{p['id']}", json={"name": "Default"})).status_code == 400
    r = await c.patch(f"/api/risk-profiles/{p['id']}", json={"description": "  Quando serve  "})
    assert r.json()["description"] == "Quando serve" and r.json()["max_position_pct"] == "25"
    r = await c.patch(f"/api/risk-profiles/{p['id']}", json={"short_allowed": False})
    assert r.json()["description"] == "Quando serve"
    r = await c.patch(f"/api/risk-profiles/{p['id']}", json={"description": "   "})
    assert r.json()["description"] is None
    long = await c.patch(f"/api/risk-profiles/{p['id']}", json={"description": "x" * 501})
    assert long.status_code == 400 and long.json()["details"]["field"] == "description"
    ok = await c.post("/api/risk-profiles", json={"name": "D500", "description": "x" * 500})
    assert ok.status_code == 200

    for bad in (
        {"max_position_pct": "150"}, {"max_daily_loss_pct": "0"},
        {"max_order_notional_eur": "-1"}, {"allowed_asset_classes": ["bonds"]},
        {"allowed_symbols": [""]},
    ):
        assert (await c.post("/api/risk-profiles", json={"name": "Z", **bad})).status_code == 400, bad
    assert (await c.patch(f"/api/risk-profiles/{uuid.uuid4()}", json={})).status_code == 404
    assert (await c.delete(f"/api/risk-profiles/{p['id']}")).status_code == 405


async def test_interest_rates_put_replaces_table(admin):
    c = admin.client
    table = (await c.get("/api/interest-rates")).json()
    assert {"kind": "loan", "key": "*", "annual_pct": "6"} in table and len(table) == 7

    new = [
        {"kind": "loan", "key": "EUR", "annual_pct": "4.5"},
        {"kind": "loan", "key": "*", "annual_pct": 7},
        {"kind": "borrow", "key": "crypto", "annual_pct": "12"},
    ]
    r = await c.put("/api/interest-rates", json=new)
    assert r.status_code == 200, r.text
    key = lambda x: (x["kind"], x["key"])
    assert sorted(r.json(), key=key) == [
        {"kind": "borrow", "key": "crypto", "annual_pct": "12"},
        {"kind": "loan", "key": "*", "annual_pct": "7"},
        {"kind": "loan", "key": "EUR", "annual_pct": "4.5"},
    ]
    assert len((await c.get("/api/interest-rates")).json()) == 3

    for bad in (
        [{"kind": "loan", "key": "EUR", "annual_pct": "4"}],  # no fallback
        [*new, {"kind": "loan", "key": "EUR", "annual_pct": "1"}],  # duplicate
        [*new, {"kind": "borrow", "key": "bonds", "annual_pct": "1"}],
        [*new, {"kind": "loan", "key": "usd!", "annual_pct": "1"}],
        [*new, {"kind": "swap", "key": "USD", "annual_pct": "1"}],
        [*new, {"kind": "loan", "key": "USD", "annual_pct": "-1"}],
    ):
        assert (await c.put("/api/interest-rates", json=bad)).status_code == 400, bad
    assert len((await c.get("/api/interest-rates")).json()) == 3  # unchanged


async def test_app_settings(admin):
    c = admin.client
    got = (await c.get("/api/settings/app")).json()
    assert got["stale_quote_s"]["crypto"] == 60 and got["default_spread_bps"]["forex"] == 1
    assert got["timezone"] == "Europe/Rome" and got["mcp_hide_mode"] is True

    r = await c.put("/api/settings/app", json={"mcp_hide_mode": False})
    assert r.status_code == 200 and r.json()["mcp_hide_mode"] is False
    assert (await c.put("/api/settings/app", json={"mcp_hide_mode": "no"})).status_code in (400, 422)
    await c.put("/api/settings/app", json={"mcp_hide_mode": True})

    r = await c.put("/api/settings/app", json={
        "stale_quote_s": {"crypto": 30}, "default_spread_bps": {"eu_equity": 8},
        "timezone": "UTC",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stale_quote_s"] == {**got["stale_quote_s"], "crypto": 30}
    assert body["default_spread_bps"] == {**got["default_spread_bps"], "eu_equity": 8}
    assert body["timezone"] == "UTC" and (await c.get("/api/settings/app")).json() == body

    from smtrading.risk.settings import get_stale_threshold

    async with admin.sm() as s:  # the engine reads the stored values
        assert await get_stale_threshold(s, "crypto") == 30

    for bad in (
        {"stale_quote_s": {"crypto": 0}}, {"stale_quote_s": {"bonds": 5}},
        {"stale_quote_s": {"crypto": -5}}, {"default_spread_bps": {"forex": -1}},
        {"default_spread_bps": {"forex": 99999}}, {"timezone": "Mars/Base"},
        {"stale_quote_s": {"crypto": "x"}}, {"stale_quote_s": {"crypto": True}},
    ):
        assert (await c.put("/api/settings/app", json=bad)).status_code == 400, bad
    assert (await c.get("/api/settings/app")).json() == body


async def test_app_settings_weekly_goal_and_preopen(admin):
    c = admin.client
    got = (await c.get("/api/settings/app")).json()
    assert got["weekly_goal"] == {"on": True, "pct": "2.0"}
    assert got["preopen_check"] == {"on": True, "at": "08:45"}

    r = await c.put("/api/settings/app", json={"weekly_goal": {"pct": "2.5"},
                                               "preopen_check": {"at": "09:05"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weekly_goal"] == {"on": True, "pct": "2.5"}
    assert body["preopen_check"] == {"on": True, "at": "09:05"}
    r = await c.put("/api/settings/app", json={"weekly_goal": {"on": False},
                                               "preopen_check": {"on": False}})
    body = r.json()
    assert body["weekly_goal"] == {"on": False, "pct": "2.5"}
    assert body["preopen_check"] == {"on": False, "at": "09:05"}

    from smtrading.telegram.preopen_settings import load_preopen_settings
    from smtrading.telegram.weekly_goal import load_weekly_goal

    async with admin.sm() as s:  # the worker reads the stored values
        goal = await load_weekly_goal(s)
        pre = await load_preopen_settings(s)
    assert (goal.on, goal.pct) == (False, Decimal("2.5"))
    assert (pre.on, pre.at.isoformat()) == (False, "09:05:00")

    for bad in ({"weekly_goal": {"pct": "0"}}, {"weekly_goal": {"pct": "x"}},
                {"weekly_goal": {"pct": "150"}}, {"preopen_check": {"at": "24:00"}},
                {"preopen_check": {"at": "nove"}}, {"weekly_goal": {"pct": "1.234"}}):
        assert (await c.put("/api/settings/app", json=bad)).status_code == 400, bad
    assert (await c.get("/api/settings/app")).json() == body


# --- kill switch ---------------------------------------------------------------------


async def _next_event(pubsub, type_="kill_switch", wait_s=2.0):
    """The next event of `type_` (the cancellations publish their own events first)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while loop.time() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg and json.loads(msg["data"])["type"] == type_:
            return json.loads(msg["data"])
    return None


async def test_kill_switch_publishes_event_and_cancels(admin):
    c = admin.client
    pid = (await _portfolio(c))["id"]
    assert (await c.get("/api/kill-switch")).json() == {"on": False}

    from smtrading.engine.models import OrderSide, OrderType
    from smtrading.engine.service import PlaceOrderRequest, place_order
    from smtrading.events import dispatch_post_commit

    async with admin.sm() as s:
        await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=uuid.UUID(pid), symbol="BTC-USD", side=OrderSide.buy,
            type=OrderType.limit, qty=D("0.1"), limit_price=D("1000.00"), source="admin",
        ))
        await s.commit()
        await dispatch_post_commit(s, None)

    redis = aioredis.from_url(admin.redis_url, decode_responses=True)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    try:
        r = await c.post("/api/kill-switch", json={"on": True, "cancel_open": True})
        assert r.status_code == 200, r.text
        assert r.json() == {"on": True, "canceled": 1}
        event = await _next_event(pubsub)
        assert event is not None and event["type"] == "kill_switch"
        assert event["on"] is True and event["canceled"] == 1
        assert (await c.get("/api/kill-switch")).json() == {"on": True}

        r = await c.post("/api/kill-switch", json={"on": False})
        assert r.json() == {"on": False, "canceled": 0}
        event = await _next_event(pubsub)
        assert event is not None and event["on"] is False
        assert (await c.get("/api/kill-switch")).json() == {"on": False}
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def test_config_routes_require_admin(admin):
    c = admin.client
    c.cookies.clear()
    for method, url in (
        ("GET", "/api/agents"), ("GET", "/api/mcp-audit"), ("GET", "/api/fee-rules"),
        ("GET", "/api/risk-profiles"), ("GET", "/api/interest-rates"),
        ("GET", "/api/settings/app"), ("GET", "/api/kill-switch"),
        ("POST", "/api/kill-switch"),
    ):
        r = await c.request(method, url, json={"on": True} if method == "POST" else None)
        assert r.status_code == 401, url


async def test_agent_save_returns_name_warning(admin):
    """Create and patch of an agent carry the name warning (saving is never blocked)."""
    c = admin.client
    warned = await _agent(c, "smoke-bot")
    assert warned["agent"]["name"] == "smoke-bot"
    assert warned["name_warning"] == "gli agenti vedranno questo nome"
    neutral = await _agent(c, "Orion")
    assert neutral["name_warning"] is None

    r = await c.patch(f"/api/agents/{warned['agent']['id']}", json={"can_notify": True})
    assert r.status_code == 200, r.text
    assert r.json()["name_warning"] == "gli agenti vedranno questo nome"
    r = await c.patch(f"/api/agents/{neutral['agent']['id']}", json={"can_notify": True})
    assert r.json()["name_warning"] is None


async def test_risk_profile_save_returns_name_warning(admin):
    c = admin.client
    r = await c.post("/api/risk-profiles", json={"name": "Demo 2"})
    assert r.status_code == 200, r.text
    assert r.json()["name_warning"] == "gli agenti vedranno questo nome"
    pid = r.json()["id"]
    r = await c.patch(f"/api/risk-profiles/{pid}", json={"name": "Alpha"})
    assert r.status_code == 200 and r.json()["name_warning"] is None
    r = await c.patch(f"/api/risk-profiles/{pid}", json={"name": "Sandbox"})
    assert r.json()["name"] == "Sandbox"
    assert r.json()["name_warning"] == "gli agenti vedranno questo nome"
