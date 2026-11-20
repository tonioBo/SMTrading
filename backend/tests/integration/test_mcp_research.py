"""MCP research tools (run_walk_forward, run_param_sweep, run_portfolio_backtest,
get_research, list_research) on the live fixture server, with the worker's
ResearchRunner."""

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.service import assign_portfolio, create_agent
from smtrading.backtest.models import ResearchRun
from smtrading.backtest.research_runner import ResearchRunner
from smtrading.common.db import make_engine
from smtrading.common.fingerprints import FINGERPRINT
from smtrading.marketdata.types import Bar
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import RiskProfile
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
TOOLS = {"run_walk_forward", "run_param_sweep", "run_portfolio_backtest", "get_research",
         "list_research"}
BASE = {"symbols": [SYM], "from_date": "2023-01-01", "to_date": "2024-12-31",
        "initial_cash": "10000"}
WF = {**BASE, "strategy": "sma_cross", "grid": {"fast": [5, 10], "slow": [30, 60]},
      "windows": 3}
SWEEP = {**BASE, "strategy": "sma_cross", "grid": {"fast": [5, 10, 20], "slow": [30, 60]}}
PF = {"sleeves": [{"strategy": "sma_cross", "params": {"fast": 5, "slow": 30},
                   "symbols": [SYM]},
                  {"strategy": "momentum", "params": {"lookback": 20}, "symbols": [SYM]}],
      "from_date": "2023-01-01", "to_date": "2024-12-31", "initial_cash": "10000"}


def _bars() -> list[Bar]:
    out, p = [], 100.0
    for i in range(700):
        o = p
        p *= 1 + 0.0005 + 0.02 * math.sin(i / 9) * 0.5
        out.append(Bar(ts=datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i),
                       open=D(f"{o:.4f}"), high=D(f"{max(o, p) * 1.01:.4f}"),
                       low=D(f"{min(o, p) * 0.99:.4f}"), close=D(f"{p:.4f}"), volume=D(1)))
    return out


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        await s.commit()
    await engine.dispose()
    mcp_server.qs.set_quote(SYM, D(49999), D(50001), D(50000))
    mcp_server.qs.set_bars(SYM, _bars())

    async def call(tool, args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token, tool, args or {})

    async def work():
        engine = make_engine(mcp_server.db_url)
        try:
            return await ResearchRunner(async_sessionmaker(engine, expire_on_commit=False),
                                        mcp_server.qs).run_once()
        finally:
            await engine.dispose()

    async def agent(name, permission=None):
        engine = make_engine(mcp_server.db_url)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            a, token = await create_agent(s, name)
            if permission:
                await assign_portfolio(s, a.id, mcp_server.demo_id, permission)
            await s.commit()
        await engine.dispose()
        return token

    return {"call": call, "work": work, "agent": agent, "server": mcp_server}


def _clean(payload) -> None:
    """No simulation word in what the agents read; `agent` is the fixture agent's name
    («test-agent»: data an admin chose, like in test_mcp_no_fingerprints)."""
    rows = payload if isinstance(payload, list) else [payload]
    text = json.dumps([{k: v for k, v in r.items() if k != "agent"} for r in rows])
    assert not FINGERPRINT.findall(text), FINGERPRINT.findall(text)


async def test_tools_listed(mcp_server, mcp_list_tools):
    assert TOOLS <= set(await mcp_list_tools(mcp_server.url, mcp_server.token))


async def test_walk_forward_flow_and_read_access(env):
    call, server = env["call"], env["server"]
    err, out = await call("run_walk_forward", {**WF, "portfolio_id": str(server.demo_id)})
    assert not err and out["status"] == "queued" and out["kind"] == "walk_forward", out
    rid = out["research_id"]
    assert await env["work"]() == 1
    err, run = await call("get_research", {"research_id": rid})
    assert not err and run["status"] == "completed", run
    res = run["result"]
    assert len(res["windows"]) == 3 and res["verdict"] in (
        "solida", "fragile", "da scartare", "dati insufficienti")
    assert set(res["windows"][0]) == {"window", "is_from", "is_to", "oos_from", "oos_to",
                                      "best_params", "is_metrics", "oos_metrics"}
    assert isinstance(res["slippage_bps"], int) and run["agent"] == "test-agent"
    _clean(run)
    err, rows = await call("list_research", {"kind": "walk_forward"})
    assert not err and [r["id"] for r in rows] == [rid]
    assert rows[0]["summary"]["verdict"] == res["verdict"] and "result" not in rows[0]
    _clean(rows)

    reader = await env["agent"]("ceo-reader", "read")  # read on the run's portfolio
    err, seen = await call("get_research", {"research_id": rid}, token=reader)
    assert not err and seen["id"] == rid
    err, rows = await call("list_research", token=reader)
    assert not err and [r["id"] for r in rows] == [rid]
    stranger = await env["agent"]("stranger")
    err, bad = await call("get_research", {"research_id": rid}, token=stranger)
    assert err and bad["code"] == "FORBIDDEN"
    err, rows = await call("list_research", token=stranger)
    assert not err and rows == []


async def test_param_sweep_flow(env):
    call = env["call"]
    err, out = await call("run_param_sweep", {**SWEEP, "sort": "robust"})
    assert not err, out
    await env["work"]()
    err, run = await call("get_research", {"research_id": out["research_id"]})
    assert not err and run["status"] == "completed", run
    res = run["result"]
    assert len(res["rows"]) == 6 and res["heatmap"]["x_param"] == "fast"
    robust = [D(r["robust"]) for r in res["rows"]]
    assert robust == sorted(robust, reverse=True)
    _clean(run)


async def test_portfolio_backtest_uses_the_risk_profile_breaker(env):
    call, server = env["call"], env["server"]
    engine = make_engine(server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        prof = RiskProfile(name="research-profile", max_daily_loss_pct=D(3))
        s.add(prof)
        await s.flush()
        p = await s.get(Portfolio, server.demo_id)
        p.risk_profile_id = prof.id
        await s.commit()
    await engine.dispose()
    err, out = await call("run_portfolio_backtest", {**PF, "portfolio_id": str(server.demo_id),
                                                     "sizing": {"max_positions": 2}})
    assert not err and out["kind"] == "portfolio", out
    await env["work"]()
    err, run = await call("get_research", {"research_id": out["research_id"]})
    assert not err and run["status"] == "completed", run
    res = run["result"]
    assert res["daily_loss_source"] == "risk_profile" and D(res["max_daily_loss_pct"]) == 3
    assert [c["sleeve"] for c in res["contribution"]] == [1, 2]
    assert res["correlation"]["labels"] == ["1:sma_cross", "2:momentum"]
    assert res["sizing"]["max_positions"] == 2
    assert {"stops", "breaker_trips", "refused_max_positions"} <= set(res["metrics"])
    _clean(run)
    err, out = await call("run_portfolio_backtest", {**PF, "max_daily_loss_pct": 1})
    await env["work"]()
    err, run = await call("get_research", {"research_id": out["research_id"]})
    assert run["result"]["daily_loss_source"] == "explicit"


@pytest.mark.parametrize("tool, args, code, field", [
    ("run_walk_forward", {**WF, "strategy": "magic"}, "INVALID_ORDER", "strategy"),
    ("run_walk_forward", {**WF, "windows": 1}, "INVALID_ORDER", "windows"),
    ("run_walk_forward", {**WF, "mode": "sideways"}, "INVALID_ORDER", "mode"),
    ("run_walk_forward", {**WF, "metric": "luck"}, "INVALID_ORDER", "metric"),
    ("run_walk_forward", {**WF, "grid": {"fast": list(range(1, 11)),
                                         "slow": list(range(30, 36))}},
     "RISK_LIMIT_EXCEEDED", None),
    ("run_walk_forward", {**WF, "symbols": [f"S{i}" for i in range(11)]},
     "RISK_LIMIT_EXCEEDED", None),
    ("run_walk_forward", {**WF, "to_date": "2031-01-01"}, "RISK_LIMIT_EXCEEDED", None),
    ("run_param_sweep", {**SWEEP, "grid": {}}, "INVALID_ORDER", "grid"),
    ("run_param_sweep", {**SWEEP, "sort": "best"}, "INVALID_ORDER", "sort"),
    ("run_param_sweep", {**SWEEP, "grid": {"fast": list(range(1, 21)),
                                           "slow": list(range(30, 41))}},
     "RISK_LIMIT_EXCEEDED", None),
    ("run_param_sweep", {**SWEEP, "symbols": [f"S{i}" for i in range(21)]},
     "RISK_LIMIT_EXCEEDED", None),
    ("run_param_sweep", {**SWEEP, "slippage_bps": 5000}, "INVALID_ORDER", "slippage_bps"),
    ("run_portfolio_backtest", {**PF, "sleeves": []}, "INVALID_ORDER", "sleeves"),
    ("run_portfolio_backtest", {**PF, "sleeves": PF["sleeves"] * 3}, "RISK_LIMIT_EXCEEDED",
     None),
    ("run_portfolio_backtest", {**PF, "sizing": {"risk_pct": 9}}, "INVALID_ORDER",
     "sizing.risk_pct"),
    ("run_portfolio_backtest", {**PF, "max_daily_loss_pct": -1}, "INVALID_ORDER",
     "max_daily_loss_pct"),
    ("run_portfolio_backtest", {**PF, "portfolio_id": "5f0e7c1e-0000-4000-8000-000000000000"},
     "FORBIDDEN", None),
    ("get_research", {"research_id": "x"}, "INVALID_ORDER", "research_id"),
    ("get_research", {"research_id": "5f0e7c1e-0000-4000-8000-000000000000"}, "NOT_FOUND",
     None),
    ("list_research", {"kind": "magic"}, "INVALID_ORDER", "kind"),
    ("list_research", {"limit": 500}, "INVALID_ORDER", "limit"),
])
async def test_argument_errors_and_limits(env, tool, args, code, field):
    err, bad = await env["call"](tool, args)
    assert err and bad["code"] == code, bad
    if field:
        assert bad["details"]["field"] == field
    _clean(bad)


async def test_rate_limit_counts_research_runs_apart_from_backtests(env):
    call, server = env["call"], env["server"]
    for args in (WF, SWEEP):
        err, out = await call("run_walk_forward" if args is WF else "run_param_sweep", args)
        assert not err, out
    err, bad = await call("run_portfolio_backtest", PF)
    assert err and bad["code"] == "RATE_LIMITED"
    err, out = await call("run_backtest", {**BASE, "strategy": "buy_and_hold"})
    assert not err and out["status"] == "queued"
    engine = make_engine(server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        assert len((await s.scalars(select(ResearchRun))).all()) == 2
    await engine.dispose()


async def test_failed_run_has_an_error(env):
    call, server = env["call"], env["server"]
    server.qs.set_bars(SYM, _bars()[:20])  # too few bars for 3 windows
    err, out = await call("run_walk_forward", WF)
    assert not err
    await env["work"]()
    err, run = await call("get_research", {"research_id": out["research_id"]})
    assert run["status"] == "failed" and "windows" in run["error"]
    _clean(run)
