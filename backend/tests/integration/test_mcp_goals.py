"""MCP get_goals: the progress of the active goals visible to the agent (plan 14) — the
total goals plus the goals of its own portfolios, never another agent's."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents import service as agents
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.goals import service as goals
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
TODAY = date(2027, 3, 10)  # a Wednesday
NOW = datetime(2027, 3, 10, 10, 0, tzinfo=ROME).astimezone(UTC)
FIELDS = {"name", "horizon", "scope", "portfolio_id", "portfolio_name", "kind",
          "period_start", "period_end", "start_eur", "current_eur", "target_eur", "pct",
          "target_pct", "gap_eur", "elapsed_pct", "status", "as_of"}


@pytest.fixture
async def env(mcp_server, mcp_call, monkeypatch):
    from smtrading.mcp import tools_goals

    monkeypatch.setattr(tools_goals, "_now", lambda: NOW)
    engine = make_engine(mcp_server.db_url)

    async def call(tool, args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token, tool, args or {})

    def db():
        return AsyncSession(engine, expire_on_commit=False)

    yield {"call": call, "server": mcp_server, "db": db}
    await engine.dispose()


async def _portfolio(env, name: str):
    async with env["db"]() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(0))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _goal(env, **body):
    async with env["db"]() as s:
        g = await goals.create_goal(s, body, today=date(2027, 3, 1))
        await s.commit()
        return g


async def _snap(env, pid, day: int, hour: int, equity: str, kind=SnapshotKind.periodic):
    ts = datetime(2027, 3, day, hour, tzinfo=ROME).astimezone(UTC)
    async with env["db"]() as s:
        s.add(EquitySnapshot(
            portfolio_id=pid, ts=ts, kind=kind, equity_eur=D(equity), cash_eur=D(equity),
            exposure_eur=D(0), margin_used_eur=D(0),
            local_date=ts.astimezone(ROME).date() if kind == SnapshotKind.day_open else None))
        await s.commit()


async def _second_agent(env, name: str, pid, permission="read") -> str:
    async with env["db"]() as s:
        agent, token = await agents.create_agent(s, name)
        if pid is not None:
            await agents.assign_portfolio(s, agent.id, pid, permission)
        await s.commit()
    return token


async def test_get_goals_total_and_own_portfolios_only(env):
    demo = env["server"].demo_id
    other = await _portfolio(env, "Altro")
    await _goal(env, name="marzo", horizon="month", target_pct="5")
    await _goal(env, name="demo", horizon="week", target_pct="1", recurring=True,
                scope="portfolio",
                portfolio_id=str(demo))
    await _goal(env, name="segreto", horizon="week", target_pct="3", recurring=True,
                scope="portfolio",
                portfolio_id=str(other))
    await _snap(env, demo, 8, 0, "1000", SnapshotKind.day_open)
    await _snap(env, demo, 10, 9, "1010")
    await _snap(env, other, 8, 0, "5000", SnapshotKind.day_open)

    err, res = await env["call"]("get_goals")
    assert not err, res
    items = res["goals"]
    assert [(g["name"], g["scope"]) for g in items] == [
        ("settimanale", "total"), ("demo", "portfolio"), ("marzo", "total")]
    assert all(set(g) == FIELDS for g in items)  # no internal goal id
    demo_goal = items[1]
    assert demo_goal["portfolio_id"] == str(demo) and demo_goal["portfolio_name"] == "Demo"
    assert (demo_goal["period_start"], demo_goal["period_end"]) == ("2027-03-08",
                                                                  "2027-03-14")
    assert (demo_goal["start_eur"], demo_goal["current_eur"], demo_goal["target_eur"],
            demo_goal["pct"], demo_goal["gap_eur"], demo_goal["target_pct"],
            demo_goal["status"]) == ("1000", "1010", "1010", "1", "0", "1", "raggiunto")
    # the totals include the live portfolios with data in the period (as the portal does)
    assert items[0]["start_eur"] == "6000"
    text = str(res)
    assert "segreto" not in text and str(other) not in text


async def test_get_goals_never_shows_another_agents_portfolio(env):
    demo = env["server"].demo_id
    beta = await _portfolio(env, "Beta")
    await _goal(env, name="demo", horizon="week", target_pct="1", recurring=True,
                scope="portfolio",
                portfolio_id=str(demo))
    await _goal(env, name="beta", horizon="month", target_pct="4", kind="cap",
                scope="portfolio", portfolio_id=str(beta))
    token = await _second_agent(env, "Vega", beta)

    err, res = await env["call"]("get_goals", token=token)
    assert not err, res
    assert [(g["name"], g["portfolio_id"], g["kind"]) for g in res["goals"]] == [
        ("settimanale", None, "min"), ("beta", str(beta), "cap")]
    assert str(demo) not in str(res)

    err, res = await env["call"]("get_goals")
    assert not err
    assert [g["name"] for g in res["goals"]] == ["settimanale", "demo"]


async def test_get_goals_hides_archived_portfolios_and_inactive_goals(env):
    demo = env["server"].demo_id
    old = await _portfolio(env, "Vecchio")
    async with env["db"]() as s:
        await agents.assign_portfolio(s, env["server"].agent_id, old, "read")
        await s.commit()
    await _goal(env, name="vecchio", horizon="week", target_pct="1", recurring=True,
                scope="portfolio",
                portfolio_id=str(old))
    await _goal(env, name="spento", horizon="week", target_pct="1", recurring=True,
                scope="portfolio",
                portfolio_id=str(demo), active=False)
    async with env["db"]() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == old)
                        .values(status=PortfolioStatus.archived))
        await s.commit()

    err, res = await env["call"]("get_goals")
    assert not err, res
    assert [g["name"] for g in res["goals"]] == ["settimanale"]
    assert res["goals"][0]["status"] == "non iniziato"  # no data in the period yet


async def test_get_goals_needs_a_portfolio(env):
    token = await _second_agent(env, "Nessuno", None)
    err, res = await env["call"]("get_goals", token=token)
    assert err and res["code"] == "FORBIDDEN", res
