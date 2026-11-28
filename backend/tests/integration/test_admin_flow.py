"""«Fiume dei flussi» admin routes: GET /api/flow/map, /api/flow/events, /api/flow/summary.
Admin only (a viewer gets 403), read only, the MCP catalog unchanged."""

from datetime import UTC, datetime, timedelta

from test_admin_users import _app_routes, env  # noqa: F401  (env: the fixture)

from smtrading.agents.models import Agent, McpAuditLog


async def _admin(env):  # noqa: F811
    email, _ = await env.add_user("admin")
    return await env.client(email)


async def test_admin_reads_the_three_routes(env):  # noqa: F811
    async with env.sm() as s:
        agent = Agent(name="Risk Manager", token_prefix="flw00001", token_hash="x")
        s.add(agent)
        await s.flush()
        at = datetime.now(UTC) - timedelta(seconds=30)
        row = McpAuditLog(agent_id=agent.id, method="get_forecast",
                          params={"symbol": "SPY", "horizon": 5}, outcome="ok",
                          latency_ms=1000, created_at=at)
        s.add(row)
        await s.commit()
    c = await _admin(env)

    r = await c.get("/api/flow/map")
    assert r.status_code == 200, r.text
    nodes = {n["key"]: n for n in r.json()["nodes"]}
    assert nodes[f"agent:{agent.id}"]["label"] == "Risk Manager"
    assert ["pc", "mcp"] in r.json()["links"]

    r = await c.get("/api/flow/events")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"events", "cursor", "now"}
    [e] = body["events"]
    assert e["id"] == f"mcp:{row.id}" and e["symbol"] == "SPY"
    assert datetime.fromisoformat(e["ts"]) == at - timedelta(seconds=1)
    assert body["cursor"] == e["ts"]
    assert "horizon" not in r.text

    r = await c.get("/api/flow/events", params={"after": body["cursor"], "limit": 10})
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["events"]] == [e["id"]]  # inside the 10 s overlap
    later = (datetime.fromisoformat(e["ts"]) + timedelta(seconds=20)).isoformat()
    r = await c.get("/api/flow/events", params={"after": later})
    assert r.json()["events"] == [] and r.json()["cursor"] == later

    for w in (1, 5, 15):
        r = await c.get("/api/flow/summary", params={"window": w})
        assert r.status_code == 200, r.text
        assert set(r.json()) == {"links", "nodes", "head"}
    r = await c.get("/api/flow/summary")
    assert r.json()["links"]["mcp|ng"]["calls"] == 1


async def test_bad_parameters_are_422(env):  # noqa: F811
    c = await _admin(env)
    for params in ({"window": 7}, {"window": "x"}):
        r = await c.get("/api/flow/summary", params=params)
        assert r.status_code == 422, (params, r.text)
    for params in ({"limit": 0}, {"limit": 2001}, {"limit": "x"}, {"after": "ieri"}):
        r = await c.get("/api/flow/events", params=params)
        assert r.status_code == 422, (params, r.text)


async def test_viewer_is_refused(env):  # noqa: F811
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    for path in ("/api/flow/map", "/api/flow/events", "/api/flow/summary"):
        assert (await c.get(path)).status_code == 403, path


async def test_mcp_unchanged_with_the_flow_router():
    from smtrading.mcp.catalog import catalog_fingerprint
    from smtrading.mcp.server import build_mcp

    assert {("GET", "/api/flow/map"), ("GET", "/api/flow/events"),
            ("GET", "/api/flow/summary")} <= set(_app_routes())
    assert await catalog_fingerprint(build_mcp()) == (
        "2eebdc17e8c7d2ea9e12e8e5c7fa08bdc6b20837d4725921f3bfab263d5d7bf7")
    names = [t.name for t in await build_mcp().list_tools()]
    assert not [n for n in names if "flow" in n]
