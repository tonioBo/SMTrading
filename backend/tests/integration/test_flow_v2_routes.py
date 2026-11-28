"""«Fiume dei flussi» v2 admin routes: replay window, «oggi», order and MCP stories, agent
runs and costs read from Paperclip (mocked with respx: cached, the key never in an answer,
Paperclip down or slow → «costi non disponibili» and still 200), node health, tool
statistics and price sources (stream status fixture in Redis). Admin only; the MCP catalog
unchanged."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from redis.asyncio import Redis
from test_admin_users import _app_routes, env  # noqa: F401  (env: the fixture)

from smtrading.agents.models import Agent, McpAuditLog
from smtrading.brokers.models import BrokerLink
from smtrading.connectors.models import Connector
from smtrading.flow import cache as flow_cache
from smtrading.marketdata import quota
from smtrading.marketdata.streaming import STATUS_KEY

BASE = "https://paperclip.example.test"
CID = "comp-1"
KEY = "pcp_board_CANARY0001"
API = f"{BASE}/api/companies/{CID}"

NEW_ROUTES = {("GET", "/api/flow/order/{order_id}"), ("GET", "/api/flow/mcp/{audit_id}"),
              ("GET", "/api/flow/agents"), ("GET", "/api/flow/health"),
              ("GET", "/api/flow/tools"), ("GET", "/api/flow/sources")}


@pytest.fixture(autouse=True)
def _fresh_caches():
    flow_cache.clear_all()
    yield
    flow_cache.clear_all()


@pytest.fixture
async def redis(env):  # noqa: F811
    r = Redis.from_url(env.redis_url)
    await r.delete(STATUS_KEY)
    yield r
    await r.delete(STATUS_KEY)
    await r.aclose()


async def _admin(env):  # noqa: F811
    email, _ = await env.add_user("admin")
    return await env.client(email)


async def _agent(env, name="Risk Manager"):  # noqa: F811
    async with env.sm() as s:
        a = Agent(name=name, token_prefix=uuid.uuid4().hex[:8], token_hash="x")
        s.add(a)
        await s.commit()
        return a


async def _configure_paperclip(c):
    r = await c.put("/api/paperclip/config", json={
        "base_url": BASE, "company_id": CID, "board_key": KEY, "enabled": True,
        "agent_map": {}})
    assert r.status_code == 200, r.text


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _runs(now):
    return [
        {"id": "r1", "agentId": "pa1", "status": "succeeded", "invocationSource": "timer",
         "startedAt": _iso(now - timedelta(minutes=10)),
         "finishedAt": _iso(now - timedelta(minutes=9)),
         "usageJson": {"billingType": "metered_api", "costUsd": 0.5}},
        {"id": "r2", "agentId": "pa1", "status": "failed", "invocationSource": "assignment",
         "startedAt": _iso(now - timedelta(minutes=5)),
         "usageJson": {"billingType": "subscription_included", "costUsd": 1.0}},
        {"id": "r3", "agentId": "pa1", "status": "cancelled", "invocationSource": "on_demand",
         "contextSnapshot": {"wakeReason": "issue_comment_mentioned"},
         "startedAt": _iso(now - timedelta(minutes=2))},
        {"id": "r9", "agentId": "pa-ghost", "status": "succeeded", "invocationSource": "timer",
         "startedAt": _iso(now - timedelta(minutes=1))},
    ]


PC_AGENTS = [{"id": "pa1", "name": "Risk Manager", "status": "idle",
              "budgetMonthlyCents": 5000, "spentMonthlyCents": 1234},
             {"id": "pa-ghost", "name": "Ghost", "status": "running"}]


async def test_agents_runs_and_costs_cached_without_the_key(env):  # noqa: F811
    c = await _admin(env)
    agent = await _agent(env)
    await _configure_paperclip(c)
    now = datetime.now(UTC)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        agents_route = router.get(f"{API}/agents").mock(
            return_value=httpx.Response(200, json=PC_AGENTS))
        runs_route = router.get(f"{API}/heartbeat-runs").mock(
            return_value=httpx.Response(200, json=_runs(now)))
        r = await c.get("/api/flow/agents")
        assert r.status_code == 200, r.text
        body = r.json()
        again = await c.get("/api/flow/agents")
        assert again.status_code == 200
        assert agents_route.call_count == 1 and runs_route.call_count == 1  # cached
        assert agents_route.calls[0].request.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in r.text and "CANARY" not in r.text
    assert set(body) == {"available", "note", "read_at", "currency", "agents", "runs"}
    assert body["available"] is True and body["note"] is None and body["currency"] == "USD"
    [a] = body["agents"]
    assert a["key"] == f"agent:{agent.id}" and a["name"] == "Risk Manager"
    assert a["paperclip_id"] == "pa1" and a["status"] == "idle"
    assert a["status_it"] == "in attesa"
    assert a["today_usd"] == "0.5" and a["month_usd"] == "12.34" and a["cap_usd"] == "50"
    assert [x["id"] for x in a["runs"]] == ["r3", "r2", "r1"]  # newest first
    runs = {e["id"]: e for e in body["runs"]}
    assert set(runs) == {"run:r1", "run:r2", "run:r3"}  # the unmatched agent has no node
    r1, r2, r3 = runs["run:r1"], runs["run:r2"], runs["run:r3"]
    assert r1["path"] == ["pc", f"agent:{agent.id}"] and r1["kind"] == "run"
    assert r1["who"] == "pc" and r1["hops"] == ["programmata"]
    assert r1["outcome"] == "ok" and r1["latency_ms"] == 60_000
    assert r1["reason"] == "timer" and r1["run_outcome"] == "riuscita"
    assert r2["hops"] == ["task"] and r2["outcome"] == "error"
    assert r2["run_outcome"] == "errore" and r2["latency_ms"] is None
    assert r3["hops"] == ["menzione"] and r3["run_outcome"] == "annullata"
    assert r3["outcome"] == "ok"
    assert [e["id"] for e in body["runs"]] == ["run:r1", "run:r2", "run:r3"]  # oldest first


@pytest.mark.parametrize("failure", ["down", "slow"])
async def test_agents_when_paperclip_is_down_or_slow(env, failure):  # noqa: F811
    c = await _admin(env)
    await _agent(env)
    await _configure_paperclip(c)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        if failure == "down":
            router.get(f"{API}/agents").mock(return_value=httpx.Response(503))
        else:
            router.get(f"{API}/agents").mock(side_effect=httpx.ReadTimeout("slow"))
        router.get(f"{API}/heartbeat-runs").mock(return_value=httpx.Response(200, json=[]))
        r = await c.get("/api/flow/agents")
        h = await c.get("/api/flow/health")
        m = await c.get("/api/flow/map")
    assert r.status_code == 200 and h.status_code == 200 and m.status_code == 200
    body = r.json()
    assert body["available"] is False and body["runs"] == []
    assert body["note"].endswith("costi non disponibili")
    [a] = body["agents"]
    assert a["today_usd"] is None and a["month_usd"] is None and a["cap_usd"] is None
    assert a["runs"] == [] and a["status"] is None
    node = h.json()["nodes"][a["key"]]
    assert node["state"] == "sconosciuto" and "Paperclip" in node["reason"]
    assert KEY not in r.text + h.text


async def test_agents_without_paperclip_configured(env):  # noqa: F811
    c = await _admin(env)
    await _agent(env)
    r = await c.get("/api/flow/agents")
    assert r.status_code == 200
    assert r.json()["available"] is False and "non disponibili" in r.json()["note"]


async def test_health_of_every_node(env, redis, monkeypatch):  # noqa: F811
    from smtrading.common.config import get_settings
    monkeypatch.setenv("SMT_NEUROGRAPH_URL", "http://ng.example.test/api/serie")
    get_settings.cache_clear()
    c = await _admin(env)
    agent = await _agent(env)
    async with env.sm() as s:
        s.add(BrokerLink(name="Paper", kind="alpaca_paper", status="error",
                         last_error="401 non autorizzato"))
        s.add(BrokerLink(name="Demo", kind="etoro_demo", status="ok"))
        s.add(McpAuditLog(agent_id=agent.id, method="get_forecast", params={},
                          outcome="ok", latency_ms=100))
        await s.commit()
    await redis.set(STATUS_KEY, json.dumps({
        "streams": {
            "alpaca:us_equity": {"symbols": ["AAPL"], "state": "connected", "note": None},
            "kraken:crypto": {"symbols": ["BTC/USD"], "state": "backoff",
                              "note": "Rifiutato dal provider (406)"}},
        "polling": ["MSFT"], "owner": True}))
    r = await c.get("/api/flow/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"checked_at", "nodes"}
    nodes = body["nodes"]
    keys = {n["key"] for n in (await c.get("/api/flow/map")).json()["nodes"]}
    assert set(nodes) == keys
    for n in nodes.values():
        assert set(n) == {"state", "reason"}
        assert n["state"] in {"ok", "attenzione", "errore", "sconosciuto"} and n["reason"]
    assert nodes["broker:alpaca_paper"]["state"] == "errore"
    assert "401" in nodes["broker:alpaca_paper"]["reason"]
    assert nodes["broker:etoro_demo"]["state"] == "ok"
    assert nodes["px"]["state"] == "attenzione" and "Kraken" in nodes["px"]["reason"]
    assert nodes["ng"]["state"] == "ok" and "previsione" in nodes["ng"]["reason"]
    assert nodes[f"agent:{agent.id}"]["state"] == "sconosciuto"
    assert nodes["db"]["state"] in {"ok", "attenzione"}
    # cached 15 s: a change is not seen at once
    await redis.delete(STATUS_KEY)
    assert (await c.get("/api/flow/health")).json()["nodes"]["px"] == nodes["px"]


async def test_sources_of_prices(env, redis):  # noqa: F811
    c = await _admin(env)
    async with env.sm() as s:
        for name in ("alpaca", "kraken", "yahoo"):
            await s.merge(Connector(name=name, enabled=True, health_status="ok"))
        await s.merge(Connector(name="finnhub", enabled=False))
        await s.commit()
    await redis.set(STATUS_KEY, json.dumps({
        "streams": {
            "alpaca:us_equity": {"symbols": ["AAPL", "SPY"], "state": "connected",
                                 "note": None},
            "kraken:crypto": {"symbols": ["BTC/USD"], "state": "backoff",
                              "note": "Rifiutato dal provider (406)"}},
        "polling": ["MSFT"], "owner": True}))
    await quota.count(redis, "yahoo", 3)
    r = await c.get("/api/flow/sources")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"checked_at", "stream_status", "polling", "sources", "disabled"}
    assert body["stream_status"] is True and body["polling"] == ["MSFT"]
    src = {s["key"]: s for s in body["sources"]}
    assert list(src)[:2] == ["alpaca", "kraken"] and "yahoo" in src  # streams first
    assert src["alpaca"]["label"] == "Alpaca" and src["alpaca"]["mode"] == "stream"
    assert src["alpaca"]["state"] == "live" and src["alpaca"]["state_it"] == "in diretta"
    assert src["alpaca"]["symbols"] == 2 and src["alpaca"]["symbol_list"] == ["AAPL", "SPY"]
    assert src["alpaca"]["updates_per_min"] is None  # no counter of stream ticks
    assert src["alpaca"]["streams"] == [{"asset_class": "us_equity", "state": "live",
                                         "symbols": 2, "note": None}]
    assert src["kraken"]["state"] == "refused" and src["kraken"]["state_it"] == "rifiutata"
    assert src["kraken"]["note"] == "Rifiutato dal provider (406)"
    assert src["yahoo"]["mode"] == "request" and src["yahoo"]["state"] == "on_demand"
    assert src["yahoo"]["state_it"] == "a richiesta" and src["yahoo"]["reads_minute"] >= 3
    assert "Finnhub" in body["disabled"]


async def test_replay_route_and_bad_windows(env):  # noqa: F811
    c = await _admin(env)
    agent = await _agent(env)
    now = datetime.now(UTC)
    async with env.sm() as s:
        for i in range(5):
            s.add(McpAuditLog(agent_id=agent.id, method="get_quote", params={},
                              outcome="ok", latency_ms=0,
                              created_at=now - timedelta(minutes=30) + i * timedelta(seconds=1)))
        await s.commit()
    frm, to = (now - timedelta(hours=1)).isoformat(), now.isoformat()
    got, cursor, pages = [], None, 0
    while True:
        params = {"from": frm, "to": to, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await c.get("/api/flow/events", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"events", "cursor", "from", "to", "now"}
        got += [e["id"] for e in body["events"]]
        cursor, pages = body["cursor"], pages + 1
        if cursor is None:
            break
    assert len(got) == 5 == len(set(got)) and pages == 3
    bad = [{"from": frm}, {"to": to}, {"from": to, "to": frm},
           {"from": (now - timedelta(hours=25)).isoformat(), "to": to},
           {"from": "ieri", "to": to}, {"from": frm, "to": to, "after": to},
           {"from": frm, "to": to, "cursor": "garbage"}, {"cursor": "x"},
           {"from": frm, "to": to, "limit": 0}]
    for params in bad:
        r = await c.get("/api/flow/events", params=params)
        assert r.status_code == 422, (params, r.text)
        assert "ieri" not in r.text and "garbage" not in r.text


async def test_summary_today_and_tools(env):  # noqa: F811
    c = await _admin(env)
    r = await c.get("/api/flow/summary", params={"window": "today"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"links", "nodes", "head"}
    for w in ("1", "5", "15", "today"):
        r = await c.get("/api/flow/tools", params={"window": w})
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"window", "families", "links", "other", "truncated"}
    for w in ("7", "ieri"):
        assert (await c.get("/api/flow/tools", params={"window": w})).status_code == 422
        assert (await c.get("/api/flow/summary", params={"window": w})).status_code == 422


async def test_map_serves_families_and_price_sources(env):  # noqa: F811
    c = await _admin(env)
    body = (await c.get("/api/flow/map")).json()
    assert {"nodes", "links", "families", "price_sources"} <= set(body)
    fam = {f["key"]: f for f in body["families"]}
    assert fam["messages"]["label"] == "messaggi" and "send_telegram" in fam["messages"]["tools"]
    assert {"key": "alpaca", "label": "Alpaca"} in body["price_sources"]


async def test_stories_routes(env):  # noqa: F811
    c = await _admin(env)
    agent = await _agent(env)
    async with env.sm() as s:
        row = McpAuditLog(agent_id=agent.id, method="place_order",
                          params={"symbol": "AAPL", "qty": "3"}, outcome="INVALID_ORDER",
                          latency_ms=20)
        s.add(row)
        await s.commit()
    r = await c.get(f"/api/flow/mcp/{row.id}")
    assert r.status_code == 200, r.text
    assert r.json()["reason_code"] == "INVALID_ORDER" and '"qty"' not in r.text
    assert (await c.get("/api/flow/mcp/999999999")).status_code == 404
    assert (await c.get("/api/flow/mcp/abc")).status_code == 422
    assert (await c.get(f"/api/flow/order/{uuid.uuid4()}")).status_code == 404
    assert (await c.get("/api/flow/order/not-a-uuid")).status_code == 422


async def test_viewer_is_refused_and_mcp_unchanged(env):  # noqa: F811
    from test_admin_users import ADMIN_ONLY_READS

    from smtrading.mcp.catalog import catalog_fingerprint
    from smtrading.mcp.server import build_mcp

    assert NEW_ROUTES <= ADMIN_ONLY_READS
    assert NEW_ROUTES <= set(_app_routes())
    email, _ = await env.add_user("viewer")
    v = await env.client(email)
    for path in ("/api/flow/agents", "/api/flow/health", "/api/flow/tools",
                 "/api/flow/sources", f"/api/flow/order/{uuid.uuid4()}", "/api/flow/mcp/1"):
        assert (await v.get(path)).status_code == 403, path
    assert await catalog_fingerprint(build_mcp()) == (
        "2eebdc17e8c7d2ea9e12e8e5c7fa08bdc6b20837d4725921f3bfab263d5d7bf7")


async def test_today_window_is_cached(env):  # noqa: F811
    c = await _admin(env)
    agent = await _agent(env)

    async def add_call():
        async with env.sm() as s:
            s.add(McpAuditLog(agent_id=agent.id, method="get_quote", params={},
                              outcome="ok", latency_ms=0))
            await s.commit()

    await add_call()
    first = (await c.get("/api/flow/summary", params={"window": "today"})).json()
    tools = (await c.get("/api/flow/tools", params={"window": "today"})).json()
    await add_call()
    again = (await c.get("/api/flow/summary", params={"window": "today"})).json()
    assert again == first  # cached ~15 s
    assert (await c.get("/api/flow/tools", params={"window": "today"})).json() == tools
    five = (await c.get("/api/flow/summary", params={"window": 5})).json()
    assert five["links"]["mcp|px"]["calls"] == first["links"]["mcp|px"]["calls"] + 1
