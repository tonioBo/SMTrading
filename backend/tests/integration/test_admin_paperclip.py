"""Admin API «Paperclip»: the configuration (board key masked, kept when omitted, never in
a response, the audit or the activity log), the test read, the status, the baseline
reconfirm and the «Costi agenti» page data. Paperclip is mocked (respx)."""
# ruff: noqa: F811  (the `admin` fixture of test_admin_telegram, imported then requested)

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import respx
from sqlalchemy import select
from test_admin_telegram import admin  # noqa: F401  (the fixture)

from smtrading.agents.models import Agent, AgentPortfolio, McpAuditLog
from smtrading.common.config import get_settings
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.paperclip.config import load_config, load_state
from smtrading.paperclip.models import AgentCostDay
from smtrading.paperclip.reader import PaperclipReader
from smtrading.useractivity.models import UserActivity

BASE = "https://paperclip.example.test"
CID = "comp-1"
KEY = "pcp_board_CANARY0001"
KEY2 = "pcp_board_CANARY0002"
API = f"{BASE}/api/companies/{CID}"
POLICY = {"id": "p1", "companyId": CID, "name": "Isolate Trader SMTrading",
          "description": None, "policyType": "block", "priority": 10, "enabled": True,
          "selectors": {"agentId": "0b6f3c8e-1d2a-4f5b-9c7d-111111111111",
                        "connectionIds": ["c1"]},
          "conditions": None, "config": None, "createdByAgentId": None,
          "createdByUserId": "u1", "createdAt": "2026-11-01T10:00:00.000Z",
          "updatedAt": "2026-11-01T10:00:00.000Z"}


def _respx():
    return respx.mock(assert_all_mocked=True, assert_all_called=False)


def _mock(router, policies=None, agents=None):
    router.get(f"{API}/agents").mock(return_value=httpx.Response(200, json=agents or [
        {"id": "pa1", "name": "TRADER"}, {"id": "pa2", "name": "Ghost"},
        {"id": "pa3", "name": "renamed-in-paperclip"}]))
    router.get(f"{API}/tools/policies").mock(return_value=httpx.Response(
        200, json={"policies": policies if policies is not None else [POLICY]}))
    router.get(f"{API}/heartbeat-runs").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{BASE}/api/cli-auth/me").mock(
        return_value=httpx.Response(200, json={"userId": "u1", "keyId": "k1"}))
    router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(200, json=[
        {"id": "k1", "name": "smtrading", "createdAt": "2026-11-01T10:00:00.000Z",
         "lastUsedAt": None, "revokedAt": None, "expiresAt": None}]))


async def _agent(sm, name) -> Agent:
    async with sm() as s:
        a = Agent(name=name, token_prefix=uuid.uuid4().hex[:8], token_hash="x")
        s.add(a)
        await s.commit()
        return a


async def _put(admin, **body):
    return await admin.client.put("/api/paperclip/config", json=body)


async def _configure(admin, agent_map=None):
    r = await _put(admin, base_url=BASE, company_id=CID, board_key=KEY, enabled=True,
                   agent_map=agent_map or {})
    assert r.status_code == 200, r.text
    return r


async def test_config_key_masked_and_kept_when_omitted(admin):
    r = await admin.client.get("/api/paperclip/config")
    assert r.status_code == 200 and r.json()["board_key_masked"] is None
    r = await _configure(admin)
    body = r.json()
    assert body["board_key_masked"] == "••••0001"
    assert (body["base_url"], body["company_id"], body["enabled"]) == (BASE, CID, True)
    assert KEY not in r.text and "board_key_id" not in body
    r = await _put(admin, company_id="comp-2")  # the key is not sent: kept
    assert r.status_code == 200 and r.json()["board_key_masked"] == "••••0001"
    async with admin.sm() as s:
        cfg = await load_config(s)
    assert (cfg.board_key, cfg.company_id, cfg.base_url) == (KEY, "comp-2", BASE)
    r = await _put(admin, board_key=KEY2)
    assert r.json()["board_key_masked"] == "••••0002"
    # validation never echoes the key
    r = await _put(admin, board_key="not-a-board-key-SECRETVALUE")
    assert r.status_code == 400 and "SECRETVALUE" not in r.text
    r = await _put(admin, base_url="ftp://x")
    assert r.status_code == 400
    r = await _put(admin, agent_map={"pa1": str(uuid.uuid4())})
    assert r.status_code == 400
    assert (await admin.client.get("/api/paperclip/config")).json()["board_key_masked"] == \
        "••••0002"
    async with admin.sm() as s:
        audits = list(await s.scalars(select(McpAuditLog).where(
            McpAuditLog.method == "admin:paperclip.config")))
        activity = list(await s.scalars(select(UserActivity).where(
            UserActivity.action == "paperclip.config")))
    assert len(audits) == 3
    assert activity and all(a.summary.get("board_key") in (None, "••••") for a in activity)
    dump = json.dumps([a.params for a in audits]) + json.dumps([a.summary for a in activity])
    assert KEY not in dump and KEY2 not in dump and "SECRETVALUE" not in dump
    assert sorted(a.params["board_key_changed"] for a in audits) == [False, True, True]
    # only the company change (an existing config) resets the baseline
    assert sorted(a.params["baseline_reset"] for a in audits) == [False, False, True]
    assert all("board_key_id" not in a.params for a in audits)
    async with admin.sm() as s:
        assert (await load_state(s))["rebaseline_reason"] == "config"


async def test_new_host_needs_the_board_key_again(admin):
    """The stored key is never sent to another host: a base_url on a new host (or port)
    needs `board_key` in the same request; the same host keeps it (plan 10, M1)."""
    await _configure(admin)
    for url in ("https://evil.example.org", "https://paperclip.example.test:8443",
                "https://user@evil.example.org"):
        r = await _put(admin, base_url=url)
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["details"] == {"field": "board_key"}
        assert body["message"] == ("indirizzo cambiato: reinserisci la chiave board per il "
                                   "nuovo indirizzo")
        assert KEY not in r.text
    async with admin.sm() as s:
        cfg = await load_config(s)
    assert (cfg.base_url, cfg.board_key) == (BASE, KEY)  # nothing saved
    # same host (other path, case, trailing slash): the key is kept
    r = await _put(admin, base_url="https://PAPERCLIP.example.test/x/")
    assert r.status_code == 200, r.text
    assert r.json()["board_key_masked"] == "••••0001"
    # new host with its key: saved
    r = await _put(admin, base_url="https://new.example.test", board_key=KEY2)
    assert r.status_code == 200, r.text
    async with admin.sm() as s:
        cfg = await load_config(s)
    assert (cfg.base_url, cfg.board_key) == ("https://new.example.test", KEY2)
    # clearing the URL sends the key nowhere: allowed; a URL again needs the key
    r = await _put(admin, base_url="", enabled=False)
    assert r.status_code == 200, r.text
    r = await _put(admin, base_url="https://new.example.test")
    assert r.status_code == 400 and r.json()["details"] == {"field": "board_key"}


async def test_new_origin_never_fails_open(admin):
    """The origin is (scheme, host, effective port): a scheme change needs the key again,
    a port that cannot be parsed is refused, the explicit default port is the same origin
    (plan 10, review I1)."""
    await _configure(admin)
    # an unparsable port: refused as a URL (httpx would connect to evil.com:443)
    for url in ("https://evil.com:+443", "https://paperclip.example.test:99999",
                "https://evil.com:abc", "https://:443"):
        r = await _put(admin, base_url=url)
        assert r.status_code == 400, (url, r.text)
        assert r.json()["details"] == {"field": "base_url"}, url
        assert KEY not in r.text
    # https -> http on the same host: the key would travel in clear
    r = await _put(admin, base_url="http://paperclip.example.test")
    assert r.status_code == 400 and r.json()["details"] == {"field": "board_key"}
    # the explicit default port is the same origin: the key is kept
    r = await _put(admin, base_url="https://paperclip.example.test:443/")
    assert r.status_code == 200, r.text
    assert r.json()["board_key_masked"] == "••••0001"
    async with admin.sm() as s:
        cfg = await load_config(s)
    assert cfg.board_key == KEY


def test_origin_is_scheme_host_port():
    from smtrading.paperclip.config import _origin
    assert _origin("https://h") == _origin("https://H:443/x") == ("https", "h", 443)
    assert _origin("http://h") == ("http", "h", 80)
    assert _origin("http://h") != _origin("https://h")
    for bad in ("https://evil.com:+443", "https://h:99999", "https://:443", "", None,
                "ftp://h"):
        assert _origin(bad) is None, bad


async def test_verify_reports_agents_and_matches(admin):
    trader = await _agent(admin.sm, "Trader")
    mapped = await _agent(admin.sm, "Analyst")
    await _configure(admin, agent_map={"pa3": str(mapped.id)})
    with _respx() as router:
        _mock(router)
        r = await admin.client.post("/api/paperclip/verify", json={})
        assert all(c.request.method == "GET" for c in router.calls)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["policies_count"] == 1 and body["error"] is None
    by_id = {a["id"]: a for a in body["agents"]}
    assert by_id["pa1"]["smt_agent"] == {"id": str(trader.id), "name": "Trader"}  # by name
    assert by_id["pa3"]["smt_agent"] == {"id": str(mapped.id), "name": "Analyst"}  # by map
    assert by_id["pa2"]["smt_agent"] is None
    with _respx() as router:
        router.get(url__startswith=BASE).mock(return_value=httpx.Response(401))
        r = await admin.client.post("/api/paperclip/verify", json={})
    assert r.json() == {"ok": False, "agents": [], "policies_count": 0, "error": "HTTP 401"}
    assert KEY not in r.text


async def test_baseline_reconfirm_audited(admin):
    await _configure(admin)
    reader = PaperclipReader(admin.sm, clock=lambda: datetime.now(UTC))
    with _respx() as router:
        _mock(router)
        await reader.tick()
    with _respx() as router:
        _mock(router, policies=[{**POLICY, "enabled": False}])
        await reader.tick()
    st = (await admin.client.get("/api/paperclip/status")).json()
    assert st["isolation"] == "changed" and st["changes"]["changed"] == ["p1"]
    assert st["enabled"] is True and st["consecutive_errors"] == 0
    # the first pass stored the MCP tool catalog fingerprint: no session refresh pending
    assert st["sessions"] == {"pending": False, "refreshed": 0, "waiting": [],
                              "given_up": [], "last_error": None,
                              "updated_at": st["sessions"]["updated_at"]}
    assert st["protection_error"] is None
    with _respx() as router:
        _mock(router, policies=[{**POLICY, "enabled": False}])
        r = await admin.client.post("/api/paperclip/baseline", json={})
    assert r.status_code == 200, r.text
    assert r.json()["isolation"] == "ok"
    async with admin.sm() as s:
        state = await load_state(s)
        (audit,) = list(await s.scalars(select(McpAuditLog).where(
            McpAuditLog.method == "admin:paperclip.baseline")))
    assert state["baseline"][0]["enabled"] is False and state["changes"]["changed"] == []
    assert audit.params["policies"] == 1 and audit.params["by"] == admin.email
    # an empty answer is not accepted as the new baseline
    with _respx() as router:
        _mock(router, policies=[])
        r = await admin.client.post("/api/paperclip/baseline", json={})
    assert r.status_code == 400


async def test_agent_costs_period_and_unmatched(admin):
    trader = await _agent(admin.sm, "Trader")
    await _configure(admin)
    r = await admin.client.post("/api/portfolios", json={"name": "Costi P1",
                                                         "initial_cash": "1000"})
    assert r.status_code in (200, 201), r.text
    pid = uuid.UUID(r.json()["id"])
    today = datetime.now(ZoneInfo(get_settings().tz)).date()
    async with admin.sm() as s:
        s.add(AgentPortfolio(agent_id=trader.id, portfolio_id=pid, permission="trade"))
        for days, agent, name, api in ((0, "pa1", "trader", "1.5"), (3, "pa1", "trader", "2"),
                                       (20, "pa1", "trader", "10"), (0, "pa9", "Ghost", "4")):
            s.add(AgentCostDay(day=today - timedelta(days=days), paperclip_agent_id=agent,
                               paperclip_agent_name=name, api_usd=Decimal(api),
                               subscription_usd=Decimal("0.5"), other_usd=Decimal(0), runs=2))
        s.add(Order(portfolio_id=pid, agent_id=str(trader.id), symbol="AAPL",
                    side=OrderSide.buy, type=OrderType.market, qty=Decimal(1)))
        await s.commit()
    r = await admin.client.get("/api/agent-costs", params={"period": "day"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "USD" and body["from"] == today.isoformat()
    (row,) = [a for a in body["agents"] if a["agent_id"] == str(trader.id)]
    assert (row["name"], row["paperclip_name"]) == ("Trader", "trader")
    assert Decimal(row["api_usd"]) == Decimal("1.5") and row["runs"] == 2
    assert Decimal(row["subscription_usd"]) == Decimal("0.5") and row["orders"] == 1
    assert row["portfolios"] == [{"id": str(pid), "name": "Costi P1", "permission": "trade",
                                  "return_pct": row["portfolios"][0]["return_pct"]}]
    assert [u["paperclip_agent_id"] for u in body["unmatched"]] == ["pa9"]
    assert Decimal(body["unmatched"][0]["api_usd"]) == Decimal(4)
    week = (await admin.client.get("/api/agent-costs", params={"period": "week"})).json()
    (row,) = [a for a in week["agents"] if a["agent_id"] == str(trader.id)]
    assert Decimal(row["api_usd"]) == Decimal("3.5") and row["runs"] == 4
    month = (await admin.client.get("/api/agent-costs", params={"period": "month"})).json()
    (row,) = [a for a in month["agents"] if a["agent_id"] == str(trader.id)]
    assert Decimal(row["api_usd"]) == Decimal("13.5")
    r = await admin.client.get("/api/agent-costs", params={"period": "year"})
    assert r.status_code == 400
