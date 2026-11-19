"""«Resoconto» data (reports.period): the numbers are those of the existing pages
(performance, strategies, goals, agent costs) restricted to the period and portfolios,
archived portfolios included; the PDF renders the real data; the API answers viewers."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pypdf import PdfReader
from sqlalchemy import delete, update
from test_agent_results import OPEN, _trade
from test_agent_results import env as results_env  # noqa: F401

from smtrading.activity.models import Decision
from smtrading.agents.service import create_agent
from smtrading.events import discard_post_commit
from smtrading.goals.models import BenchmarkSnapshot, Goal
from smtrading.paperclip.models import AgentCostDay
from smtrading.performance.agent_results import _strategies
from smtrading.performance.service import get_performance
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.reports.pdf import render_pdf
from smtrading.reports.period import period_report
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")


def _snap(pid, ts, equity: str, kind=SnapshotKind.periodic) -> EquitySnapshot:
    return EquitySnapshot(
        portfolio_id=pid, ts=ts, kind=kind, equity_eur=D(equity), cash_eur=D(equity),
        exposure_eur=D(0), margin_used_eur=D(0),
        local_date=ts.astimezone(ROME).date() if kind == SnapshotKind.day_open else None)


async def test_report_numbers_match_the_existing_pages(results_env):
    env = results_env
    now = datetime.now(UTC)
    today = now.astimezone(ROME).date()
    start = OPEN.astimezone(ROME).date()  # the day of the test trades
    async with env.sm() as s:
        a = await create_portfolio(s, name="Report A", initial_cash=D(2000))
        b = await create_portfolio(s, name="Report B", initial_cash=D(1000))
        other = await create_portfolio(s, name="Fuori report", initial_cash=D(1000))
        discard_post_commit(s)
        agent, _ = await create_agent(s, "CEO SMTrading")
        await s.commit()
    await _trade(env, a.id, "buy", "4", "99", "100")
    await _trade(env, a.id, "sell", "2", "110", "111")
    await _trade(env, other.id, "buy", "1", "99", "100")
    now = datetime.now(UTC)  # after the fills (stamped by the database)
    day0 = datetime.combine(start, datetime.min.time(), tzinfo=ROME).astimezone(UTC)
    async with env.sm() as s:
        # the trades' own day_open snapshots: replaced by the ones of the test
        await s.execute(delete(EquitySnapshot))
        s.add_all([
            _snap(a.id, day0 + timedelta(hours=9), "2000", SnapshotKind.day_open),
            _snap(a.id, day0 + timedelta(days=1, hours=12), "1900"),
            _snap(a.id, day0 + timedelta(days=2, hours=9), "2100", SnapshotKind.day_open),
            _snap(a.id, now - timedelta(minutes=5), "2050"),
            _snap(b.id, day0 + timedelta(days=1, hours=9), "1000", SnapshotKind.day_open),
            _snap(b.id, now - timedelta(minutes=5), "1010"),
            _snap(b.id, day0 - timedelta(days=5), "1500"),  # before the period
        ])
        s.add_all([
            AgentCostDay(day=start, paperclip_agent_id="pc-1", paperclip_agent_name="CEO",
                         api_usd=D("1.5"), subscription_usd=D("2"), other_usd=D(0), runs=3),
            AgentCostDay(day=today, paperclip_agent_id="pc-1", paperclip_agent_name="CEO",
                         api_usd=D("0.5"), subscription_usd=D(0), other_usd=D(0), runs=1),
            AgentCostDay(day=start - timedelta(days=1), paperclip_agent_id="pc-1",
                         paperclip_agent_name="CEO", api_usd=D(9), subscription_usd=D(0),
                         other_usd=D(0), runs=9),
        ])
        s.add_all([
            Decision(agent_id=agent.id, kind="lesson", portfolio_id=a.id,
                     summary="Lezione del periodo", details={}),
            Decision(agent_id=agent.id, kind="team_rule", portfolio_id=None,
                     summary="Regola del team", details={"status": "active"}),
            Decision(agent_id=agent.id, kind="veto", portfolio_id=other.id,
                     summary="Veto di un altro portafoglio", details={}),
            Decision(agent_id=agent.id, kind="strategy", portfolio_id=a.id,
                     summary="Strategia nuova", details={}),
            Decision(agent_id=agent.id, kind="lesson", portfolio_id=a.id,
                     summary="Lezione vecchia", details={},
                     created_at=day0 - timedelta(days=2)),
        ])
        goal = Goal(name="campagna", horizon="custom", start_date=start, end_date=today,
                    recurring=False, scope="portfolio", portfolio_id=a.id,
                    target_pct=D(3), kind="min", active=True, closes_positions=False)
        s.add(goal)
        s.add_all([
            BenchmarkSnapshot(symbol="SPY", ts=day0 - timedelta(hours=2), price=D(100),
                              currency="USD", fx_eur=D(1), price_eur=D(100)),
            BenchmarkSnapshot(symbol="SPY", ts=now - timedelta(minutes=10), price=D(101),
                              currency="USD", fx_eur=D(1), price_eur=D(101)),
        ])
        await s.commit()
        # archived after the period: still in the report when named
        await s.execute(update(Portfolio).where(Portfolio.id == b.id)
                        .values(status=PortfolioStatus.archived))
        await s.commit()

    now = datetime.now(UTC)  # after the decisions (stamped by the database)
    async with env.sm() as s:
        data = await period_report(s, env.qs, start=start, end=today,
                                   portfolio_ids=[a.id, b.id], now=now, zone=ROME,
                                   campaign_goal_ids=[goal.id])
        perf = await get_performance(s, a.id, "all", now=now)
        strategies, _ = await _strategies(s, env.qs, None, portfolio_ids=[a.id])

    pa, pb = data["portfolios"]
    assert (pa["name"], pb["name"], pb["archived"]) == ("Report A", "Report B", True)
    assert (pa["start_eur"], pa["end_eur"], pa["change_eur"]) == (D("2000.00"),
                                                                   D("2050.00"), D("50.00"))
    # the performance page's maths (A has no snapshot outside the period)
    assert (pa["return_pct"], pa["max_drawdown_pct"], pa["sharpe"]) == (
        perf["return_pct"], perf["max_drawdown_pct"], perf["sharpe"])
    assert pa["return_pct"] == D("2.5000") and pa["max_drawdown_pct"] == D("-5.0000")
    assert pb["return_pct"] == D("1.0000")  # its snapshot before the period left out
    t = data["total"]
    assert (t["start_eur"], t["end_eur"], t["return_pct"]) == (D("3000.00"), D("3060.00"),
                                                              D("2.0000"))
    assert [c["day"] for c in t["curve"]] == [
        start + timedelta(days=i) for i in range((today - start).days + 1)]
    assert t["curve"][0]["equity_eur"] == D("2000.00")
    assert t["curve"][1]["equity_eur"] == D("2900.00")  # A 1900 + B 1000
    spy = next(x for x in data["benchmarks"] if x["symbol"] == "SPY")
    assert spy["change_pct"] == D("1.00") and spy["diff_pp"] == D("1.00")
    assert next(x for x in data["benchmarks"] if x["symbol"] == "QQQ")["change_pct"] is None

    # operations and strategies: the strategies page's FIFO trades of A only
    ops = data["operations"]
    assert ops["fills"] == 2 and ops["trades_opened"] == 1
    assert {(x["portfolio"], x["symbol"]) for x in ops["trades"]} == {("Report A", "AAPL")}
    assert [s.realized_pnl_eur for s in data["strategies"]] == [
        s.realized_pnl_eur for s in strategies]
    assert [s.costs_eur for s in data["strategies"]] == [s.costs_eur for s in strategies]

    goals = data["goals"]
    assert [(g["name"], g["portfolio_name"], g["campaign"]) for g in goals] == [
        ("campagna", "Report A", True)]
    assert goals[0]["start_eur"] == D("2000.00") and goals[0]["pct"] == D("2.50")

    costs = data["agent_costs"]
    assert (costs["cost_usd"], costs["billed_usd"], costs["runs"]) == (D("4.00"),
                                                                       D("2.00"), 4)
    assert [x["summary"] for x in data["lessons"]] == ["Lezione del periodo"]
    assert [x["summary"] for x in data["team_rules"]] == ["Regola del team"]
    assert [x["summary"] for x in data["decisions"]] == ["Strategia nuova"]

    raw = render_pdf(data)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages)
    assert "Resoconto del periodo" in text and "Report A" in text
    assert "2.050,00 EUR" in text and "Lezione del periodo" in text
