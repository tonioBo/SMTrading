"""«Risultati agenti»: per agent (decisions by kind, proposals approved/vetoed, lessons
written and about it, MCP calls and errors, executed orders, Paperclip cost), per strategy
(trades closed in the period, win rate, net P/L, holding time, open positions) and the
team summary, from real sim fills, decisions, audit rows and cost days; a viewer reads
`GET /api/agent-results`."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents.models import McpAuditLog
from smtrading.agents.service import create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, OrderSide, OrderType
from smtrading.engine.quotesource import FakeMarketData
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import discard_post_commit
from smtrading.paperclip.models import AgentCostDay
from smtrading.performance.agent_results import agent_results
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
PASSWORD = "correct-horse-battery"
OLD = timedelta(days=200)


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))  # Demo
        await s.commit()
    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))

    class NS:
        pass

    ns = NS()
    ns.sm, ns.qs = sm, qs
    yield ns
    await engine.dispose()


async def _trade(env, pid, side, qty, bid, ask, coid=None, agent=None) -> uuid.UUID:
    env.qs.set_quote("AAPL", D(bid), D(ask), None)
    async with env.sm() as s:
        order = await place_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide(side), type=OrderType.market,
            qty=D(qty), client_order_id=coid, agent_id=agent,
        ), now=OPEN)
        quote = (await env.qs.get_quotes(["AAPL"]))["AAPL"]
        await execute_fill(s, env.qs, order.id, quote=quote)
        discard_post_commit(s)
        await s.commit()
        return order.id


async def _setup(env):
    """Two portfolios' worth of team history; returns (portfolio id, agents by role)."""
    async with env.sm() as s:
        p = await create_portfolio(s, name="Portafoglio Aggressivo", initial_cash=D(2000))
        discard_post_commit(s)
        ag = {}
        for role, name in (("ceo", "CEO SMTrading"), ("pm", "Portfolio Manager"),
                           ("rm", "Risk Manager"), ("es", "Execution Supervisor")):
            ag[role], _ = await create_agent(s, name)
        await s.commit()
        pid = p.id

        def dec(role, kind, details=None, **kw):
            return Decision(agent_id=ag[role].id, kind=kind, portfolio_id=pid,
                            summary=f"{kind}", details=details or {}, **kw)

        s.add_all([
            dec("ceo", "strategy", {"ref": "STR-7", "status": "approvata"},
                subject="rsi_reversion su AAPL"),
            dec("pm", "proposal", {"ref": "PROP-AGG-1", "strategy_id": "STR-7"}),
            dec("pm", "proposal", {"ref": "PROP-AGG-2", "strategy_id": "STR-7"}),
            dec("pm", "proposal", {"ref": "PROP-AGG-3", "strategy_id": "STR-7"}),
            dec("rm", "approval", {"ref": "PROP-AGG-1"}),
            dec("rm", "veto", {"ref": "PROP-AGG-2"}),
            dec("rm", "lesson", {"ref": "LEZ-1", "agents": ["pm", "Risk Manager"]}),
            dec("ceo", "lesson", {"ref": "LEZ-2", "agents": ["PM"], "repeats": ["LEZ-1"]}),
            dec("ceo", "lesson", {"ref": "LEZ-3", "agents": ["tutti"]}),
            dec("ceo", "team_rule", {"ref": "REG-1", "status": "active"}),
            dec("ceo", "team_rule", {"ref": "REG-2", "status": "active"}),
            dec("ceo", "team_rule", {"ref": "REG-2", "status": "retired"}),
        ])
        old = dec("pm", "proposal", {"ref": "PROP-AGG-0"})
        old_lesson = dec("ceo", "lesson", {"ref": "LEZ-0", "agents": ["pm"]})
        s.add_all([old, old_lesson])
        now = datetime.now(UTC)
        s.add_all([
            McpAuditLog(agent_id=ag["pm"].id, method="get_quote", params={}, outcome="ok",
                        latency_ms=5),
            McpAuditLog(agent_id=ag["pm"].id, method="place_order", params={},
                        outcome="INVALID_ORDER", latency_ms=5),
            McpAuditLog(agent_id=ag["pm"].id, method="get_quote", params={}, outcome="ok",
                        latency_ms=5, created_at=now - OLD),
            McpAuditLog(agent_id=None, method="admin:paperclip.baseline", params={},
                        outcome="ok", latency_ms=0),
        ])
        today = now.date()
        s.add_all([
            AgentCostDay(day=today, paperclip_agent_id="pa-pm",
                         paperclip_agent_name="portfolio manager", api_usd=D("1.5"),
                         subscription_usd=D("2"), other_usd=D("0.5"), runs=3),
            AgentCostDay(day=today, paperclip_agent_id="pa-ghost",
                         paperclip_agent_name="Ghost", api_usd=D("1"),
                         subscription_usd=D(0), other_usd=D(0), runs=1),
            AgentCostDay(day=today - OLD, paperclip_agent_id="pa-pm",
                         paperclip_agent_name="portfolio manager", api_usd=D("100"),
                         subscription_usd=D(0), other_usd=D(0), runs=9),
        ])
        await s.commit()
        await s.execute(update(Decision).where(Decision.id.in_([old.id, old_lesson.id]))
                        .values(created_at=now - OLD))
        await s.commit()
    return pid, ag


async def test_empty(env):
    async with env.sm() as s:
        r = await agent_results(s, env.qs, "week")
    assert [a.name for a in r.agents] == ["paperclip-demo"]  # the seed's agent
    assert r.agents[0].decisions_total == 0 and r.strategies == []
    assert r.team.pnl_eur == 0 and r.team.orders_executed == 0
    assert r.team.cost_usd == 0 and r.team.pnl_per_usd is None
    assert r.from_ is not None and r.from_.weekday() == 0


async def test_agents_strategies_and_team(env):
    pid, ag = await _setup(env)
    es = str(ag["es"].id)
    o1 = await _trade(env, pid, "buy", 2, "199", "200", coid="PROP-AGG-1", agent=es)
    o2 = await _trade(env, pid, "sell", 2, "250", "251", agent=es)  # closes STR-7: a win
    await _trade(env, pid, "buy", 1, "204", "205")  # admin: «Senza strategia», open

    async with env.sm() as s:
        r = await agent_results(s, env.qs, "week")
        everything = await agent_results(s, env.qs, "all")
    by = {a.name: a for a in r.agents}
    assert list(by) == ["CEO SMTrading", "Execution Supervisor", "paperclip-demo",
                        "Portfolio Manager", "Risk Manager"]  # case insensitive
    pm, rm, ceo, esr = (by["Portfolio Manager"], by["Risk Manager"], by["CEO SMTrading"],
                        by["Execution Supervisor"])
    # decisions by kind; the 200-day-old proposal is out of the week
    assert pm.decisions["proposal"] == 3 and pm.decisions_total == 3
    assert set(pm.decisions) >= {"proposal", "approval", "veto", "strategy", "risk_rule",
                                 "lesson", "team_rule", "other"}
    assert (pm.proposals, pm.approved, pm.vetoed, pm.pending) == (3, 1, 1, 1)
    assert pm.approval_rate_pct == D(50)
    assert rm.decisions["approval"] == 1 and rm.decisions["veto"] == 1
    assert rm.approval_rate_pct is None  # no proposals of its own
    # lessons: written by, about («pm», «PM», full name; «tutti» names nobody)
    assert (rm.lessons_written, ceo.lessons_written) == (1, 2)
    assert (pm.lessons_about, pm.lessons_repeated) == (2, 1)
    assert (rm.lessons_about, rm.lessons_repeated) == (1, 0)
    assert ceo.lessons_about == 0
    # MCP: the week's calls only, errors = outcome not ok; admin rows ignored
    assert (pm.mcp_calls, pm.mcp_errors, pm.mcp_error_pct) == (2, 1, D(50))
    assert ceo.mcp_calls == 0 and ceo.mcp_error_pct is None
    # cost: matched by name (case insensitive), the old day out of the week
    assert (pm.cost_usd, pm.billed_usd, pm.runs) == (D("4.00"), D("2.00"), 3)
    assert pm.paperclip_names == ["portfolio manager"]
    assert pm.orders_executed == 0 and pm.cost_per_order_usd is None
    assert esr.orders_executed == 2 and esr.cost_usd == 0

    # strategies: STR-7 closed with a win; «Senza strategia» open
    [strat, none] = r.strategies
    assert (strat.ref, strat.status, strat.portfolios) == (
        "STR-7", "approvata", ["Portafoglio Aggressivo"])
    assert (strat.trades_opened, strat.trades_closed, strat.wins) == (1, 1, 1)
    assert strat.win_rate_pct == D(100) and strat.open_positions == 0
    assert strat.realized_pnl_eur > 0 and strat.costs_eur > 0
    assert strat.net_pnl_eur == strat.realized_pnl_eur - strat.costs_eur
    assert strat.avg_holding_s is not None and strat.avg_holding_s >= 0
    assert none.ref is None and none.trades_opened == 1 and none.trades_closed == 0
    assert none.open_positions == 1 and none.net_pnl_eur == 0 and none.win_rate_pct is None

    t = r.team
    assert t.pnl_eur == strat.net_pnl_eur and t.orders_executed == 3
    assert (t.proposals, t.approvals, t.vetoes, t.approval_rate_pct) == (3, 1, 1, D(50))
    assert (t.lessons, t.lessons_repeated, t.rules_active) == (3, 1, 1)
    assert (t.cost_usd, t.billed_usd, t.unmatched_cost_usd) == (D("5.00"), D("3.00"),
                                                                D("1.00"))
    assert t.pnl_per_usd == (t.pnl_eur / D(5)).quantize(D("0.01"))
    assert t.cost_per_order_usd == (D(5) / 3).quantize(D("0.01"))
    assert {o1, o2}  # the orders exist

    # «tutto»: the old rows count too
    by_all = {a.name: a for a in everything.agents}
    assert by_all["Portfolio Manager"].proposals == 4
    assert by_all["Portfolio Manager"].pending == 2
    assert by_all["Portfolio Manager"].mcp_calls == 3
    assert by_all["Portfolio Manager"].cost_usd == D("104.00")
    assert everything.team.lessons == 4 and everything.from_ is None


async def test_closed_before_the_period_is_out(env):
    pid, _ = await _setup(env)
    await _trade(env, pid, "buy", 1, "199", "200", coid="PROP-AGG-1")
    await _trade(env, pid, "sell", 1, "250", "251")
    async with env.sm() as s:
        await s.execute(update(Fill).values(created_at=datetime.now(UTC) - OLD))
        await s.commit()
        r = await agent_results(s, env.qs, "3m")
        a = await agent_results(s, env.qs, "all")
    assert r.strategies == [] and r.team.orders_executed == 0 and r.team.pnl_eur == 0
    [strat] = a.strategies
    assert strat.trades_closed == 1 and a.team.orders_executed == 2


async def test_api_viewer_reads_and_bad_period(env, redis_url):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.admin.routes_agent_results import CACHE_KEY, CACHE_TTL_S
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime

    pid, ag = await _setup(env)
    await _trade(env, pid, "buy", 1, "199", "200", coid="PROP-AGG-1",
                 agent=str(ag["es"].id))
    email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
    async with env.sm() as s:
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()
    qs = env.qs
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    redis = Redis.from_url(redis_url)
    keys = [CACHE_KEY.format(period=p) for p in ("week", "1m", "3m", "all")]
    await redis.delete(*keys)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            r = await c.get("/api/agent-results?period=1m")
            assert r.status_code == 200, r.text
            body = r.json()
            assert 0 < await redis.ttl(CACHE_KEY.format(period="1m")) <= CACHE_TTL_S
            assert (await c.get("/api/agent-results?period=1m")).json() == body
            default = await c.get("/api/agent-results")
            assert default.status_code == 200 and default.json()["period"] == "week"
            bad = await c.get("/api/agent-results?period=year")
            assert bad.status_code == 400 and bad.json()["details"]["field"] == "period"
            assert (await c.post("/api/agent-results")).status_code in (403, 405)
        await redis.delete(*keys)
    finally:
        stop.set()
        await lifespan
        await redis.aclose()
    assert body["period"] == "1m" and body["from"] and body["to"]
    t = body["team"]
    assert t["orders_executed"] == 1 and t["proposals"] == 3 and t["rules_active"] == 1
    assert D(t["cost_usd"]) == 5 and isinstance(t["pnl_eur"], str)
    pm = next(a for a in body["agents"] if a["name"] == "Portfolio Manager")
    assert pm["decisions"]["proposal"] == 3 and D(pm["approval_rate_pct"]) == 50
    assert D(pm["cost_usd"]) == 4
    [strat] = body["strategies"]
    assert strat["ref"] == "STR-7" and strat["open_positions"] == 1
    assert strat["trades_opened"] == 1 and strat["trades_closed"] == 0
