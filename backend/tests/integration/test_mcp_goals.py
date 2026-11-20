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
from smtrading.goals.models import Goal
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
TODAY = date(2027, 3, 10)  # a Wednesday
NOW = datetime(2027, 3, 10, 10, 0, tzinfo=ROME).astimezone(UTC)
FIELDS = {"name", "horizon", "scope", "portfolio_id", "portfolio_name", "kind",
          "period_start", "period_end", "start_eur", "current_eur", "target_eur", "pct",
          "target_pct", "gap_eur", "elapsed_pct", "status", "as_of", "benchmark",
          "benchmark_pct", "diff_pp", "loss_pct", "closes_positions", "plan_end_actions"}


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
    # the agent does not see «Altro»: no EUR figure of the totals (they would reveal its
    # equity), only the percentages and the status
    total = items[0]
    assert (total["start_eur"], total["current_eur"], total["target_eur"],
            total["gap_eur"]) == (None, None, None, None)
    assert (total["pct"], total["target_pct"], total["status"]) == ("0.17", "2",
                                                                     "in ritardo")
    assert total["elapsed_pct"] is not None
    assert items[1]["start_eur"] == "1000"  # its own portfolio's goal keeps the EUR
    text = str(res)
    assert "segreto" not in text and str(other) not in text and "6000" not in text


async def test_get_goals_total_eur_when_the_agent_sees_every_portfolio(env):
    demo = env["server"].demo_id
    other = await _portfolio(env, "Altro")
    async with env["db"]() as s:
        await agents.assign_portfolio(s, env["server"].agent_id, other, "read")
        await s.commit()
    archived = await _portfolio(env, "Archiviato")  # not live: does not count
    async with env["db"]() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == archived)
                        .values(status=PortfolioStatus.archived))
        await s.commit()
    await _snap(env, demo, 8, 0, "1000", SnapshotKind.day_open)
    await _snap(env, demo, 10, 9, "1010")
    await _snap(env, other, 8, 0, "5000", SnapshotKind.day_open)

    err, res = await env["call"]("get_goals")
    assert not err, res
    [total] = res["goals"]
    # the totals include the live portfolios with data in the period (as the portal does)
    assert (total["start_eur"], total["current_eur"], total["target_eur"],
            total["gap_eur"]) == ("6000", "6010", "6120", "110")


async def test_get_goals_leaves_out_goals_over(env):
    """A goal that does not recur and whose period is over is not for the agent."""
    await _goal(env, name="febbraio", horizon="custom", target_pct="1",
                start_date="2027-02-01", end_date="2027-02-28")
    await _goal(env, name="marzo", horizon="month", target_pct="5")
    err, res = await env["call"]("get_goals")
    assert not err, res
    assert [g["name"] for g in res["goals"]] == ["settimanale", "marzo"]


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


async def test_get_goals_risk_and_index_goals(env):
    """max_loss and beat_index goals: loss_pct, benchmark, benchmark_pct, diff_pp; a total
    goal of an agent that does not see every portfolio keeps its percentages only."""
    from smtrading.goals.models import BenchmarkSnapshot

    demo = env["server"].demo_id
    await _portfolio(env, "Altro")
    await _goal(env, name="rischio", horizon="week", target_pct="5", kind="max_loss",
                recurring=True, scope="portfolio", portfolio_id=str(demo))
    await _goal(env, name="indice", horizon="week", target_pct="0", kind="beat_index",
                benchmark="SPY", recurring=True)
    await _snap(env, demo, 8, 0, "1000", SnapshotKind.day_open)
    await _snap(env, demo, 10, 9, "960")
    async with env["db"]() as s:
        for day, hour, eur in ((5, 22, "500"), (10, 9, "490")):
            s.add(BenchmarkSnapshot(
                symbol="SPY", ts=datetime(2027, 3, day, hour, tzinfo=ROME).astimezone(UTC),
                price=D(eur), currency="USD", fx_eur=D(1), price_eur=D(eur)))
        await s.commit()
    err, res = await env["call"]("get_goals")
    assert not err, res
    by = {g["name"]: g for g in res["goals"]}
    risk = by["rischio"]
    assert (risk["kind"], risk["pct"], risk["loss_pct"], risk["target_eur"], risk["status"],
            risk["benchmark"]) == ("max_loss", "-4", "4", "950", "vicino al limite", None)
    index = by["indice"]
    assert (index["kind"], index["benchmark"], index["pct"], index["benchmark_pct"],
            index["diff_pp"], index["status"], index["loss_pct"]) == (
        "beat_index", "SPY", "-4", "-2", "-2", "indietro", None)
    assert index["start_eur"] is None and index["target_eur"] is None


async def test_get_goals_plan_fields(env):
    # the migrated weekly goal on the total is a piano: its week ends on Sunday, no grace
    err, res = await env["call"]("get_goals")
    assert not err, res
    [weekly] = res["goals"]
    assert (weekly["name"], weekly["closes_positions"], weekly["period_end"]) == (
        "settimanale", True, "2027-03-14")
    assert weekly["plan_end_actions"] == {"summary": (
        "All'ultima seduta del periodo chiudi tutte le posizioni dei portafogli coperti "
        "nelle finestre pre-chiusura (anche quelle in perdita); quelle rimaste aperte le "
        "chiude SMTrading alla prima seduta utile. I nuovi ingressi vicino a period_end "
        "devono avere il tempo di lavorare prima della fine.")}
    assert "grazia" not in weekly["plan_end_actions"]["summary"]
    async with env["db"]() as s:
        await s.execute(update(Goal).values(closes_positions=False))
        await s.commit()
    await _goal(env, name="piano mese", horizon="month", target_pct="3", recurring=True)
    err, res = await env["call"]("get_goals")
    by = {g["name"]: g for g in res["goals"]}
    assert by["settimanale"]["closes_positions"] is False
    assert by["settimanale"]["plan_end_actions"] is None
    plan = by["piano mese"]  # a piano by default
    assert plan["closes_positions"] is True and plan["period_end"] == "2027-03-31"
    assert plan["plan_end_actions"] == weekly["plan_end_actions"]
