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
POLICY = {"id": "p1", "name": "Isolate trader SMTrading", "policyType": "deny",
          "selectors": {}, "conditions": {}, "config": {}, "priority": 1, "enabled": True}


def _respx():
    return respx.mock(assert_all_mocked=True, assert_all_called=False)


def _mock(router, policies=None, agents=None):
    router.get(f"{API}/agents").mock(return_value=httpx.Response(200, json=agents or [
        {"id": "pa1", "name": "TRADER"}, {"id": "pa2", "name": "Ghost"},
        {"id": "pa3", "name": "renamed-in-paperclip"}]))
    router.get(f"{API}/tools/policies").mock(return_value=httpx.Response(
        200, json=policies if policies is not None else [POLICY]))
    router.get(f"{API}/heartbeat-runs").mock(return_value=httpx.Response(200, json=[]))
    router.get(f"{BASE}/api/cli-auth/me").mock(return_value=httpx.Response(404))
    router.get(f"{BASE}/api/board-api-keys").mock(return_value=httpx.Response(404))


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
    assert KEY not in r.text
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
