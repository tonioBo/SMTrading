"""`GET /api/roadmap`: the curated roadmap plus the live numbers, for every portal user
(admin and viewer), read only, never MCP."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import update
from test_admin_users import _app_routes, env  # noqa: F401

from smtrading.events import discard_post_commit
from smtrading.goals.models import Goal
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
AT = datetime(2026, 11, 28, 15, 0, tzinfo=ROME).astimezone(UTC)  # day 6 of the campaign


def _clock(monkeypatch, at: datetime = AT) -> None:
    import smtrading.admin.routes_roadmap as routes

    monkeypatch.setattr(routes, "_now", lambda: at)


async def _client(env, role: str = "admin"):
    email, _ = await env.add_user(role)
    return await env.client(email)


async def _portfolio(sm, name: str, *, in_totals: bool = True):
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(0))
        discard_post_commit(s)
        await s.commit()
        if not in_totals:
            await s.execute(update(Portfolio).where(Portfolio.id == p.id)
                            .values(in_totals=False))
            await s.commit()
        return p.id


async def _snap(sm, pid, ts: datetime, equity: str, kind=SnapshotKind.periodic) -> None:
    async with sm() as s:
        s.add(EquitySnapshot(
            portfolio_id=pid, ts=ts, kind=kind, equity_eur=D(equity), cash_eur=D(equity),
            exposure_eur=D(0), margin_used_eur=D(0),
            local_date=ts.astimezone(ROME).date() if kind == SnapshotKind.day_open else None))
        await s.commit()


async def test_admin_and_viewer_read_the_same_roadmap(env, monkeypatch):
    _clock(monkeypatch)
    admin = await _client(env, "admin")
    viewer = await _client(env, "viewer")
    a = await admin.get("/api/roadmap")
    v = await viewer.get("/api/roadmap")
    assert a.status_code == 200, a.text
    assert v.status_code == 200, v.text
    assert a.json() == v.json()
    body = v.json()
    assert set(body) == {"updated", "summary", "campaign", "go_no_go", "live", "stages",
                         "glossary"}
    assert len(body["stages"]) >= 8 and body["glossary"]
    assert all(s["items"] for s in body["stages"])
    live = body["live"]
    assert live["campaign"] == {"start": "2026-11-23", "end": "2026-12-22", "day": 6,
                                "total_days": 30, "state": "in_corso"}
    assert live["go_no_go"] == {"date": "2026-12-23", "days_left": 25}
    assert live["neurograph"] == {"tools": ["get_forecast"], "count": 1}
    assert live["drills"]["planned"] >= 8
    assert 0 < live["drills"]["done"] < live["drills"]["planned"]
    assert live["drills"]["next"]["date"] == "2026-11-29"


async def test_no_goal_and_no_snapshot_is_a_dash_not_an_error(env, monkeypatch):
    _clock(monkeypatch)
    async with env.sm() as s:  # the seeded weekly goal is not the campaign goal anyway
        await s.execute(update(Goal).values(active=False))
        await s.commit()
    c = await _client(env, "viewer")
    r = await c.get("/api/roadmap")
    assert r.status_code == 200, r.text
    live = r.json()["live"]
    assert live["goal"] is None and live["equity"] is None
    assert any("obiettivo" in n.casefold() for n in live["notes"])
    assert any("valore" in n.casefold() for n in live["notes"])


async def test_equity_total_against_the_campaign_goal(env, monkeypatch):
    _clock(monkeypatch)
    p1 = await _portfolio(env.sm, "Rm Alpaca")
    p2 = await _portfolio(env.sm, "Rm eToro")
    hidden = await _portfolio(env.sm, "Rm prova nascosta", in_totals=False)
    nov1 = datetime(2026, 11, 2, 9, 0, tzinfo=ROME).astimezone(UTC)
    for pid in (p1, p2, hidden):
        await _snap(env.sm, pid, nov1, "1000", SnapshotKind.day_open)
    await _snap(env.sm, p1, AT - timedelta(minutes=5), "1030")
    await _snap(env.sm, p2, AT - timedelta(minutes=10), "1010")
    await _snap(env.sm, hidden, AT - timedelta(minutes=5), "9999")
    async with env.sm() as s:
        s.add(Goal(name="mensile", horizon="month", start_date=date(2026, 1, 1),
                   recurring=True, scope="total", target_pct=D("5"), kind="min"))
        await s.commit()
    c = await _client(env, "viewer")
    live = (await c.get("/api/roadmap")).json()["live"]
    assert live["equity"]["current_eur"] == "2040"  # decimals as in /api/goals
    assert live["equity"]["portfolios"] == 2
    g = live["goal"]
    assert g["label"] == "Mensile +5%" and g["name"] == "mensile"
    assert (g["start_eur"], g["current_eur"], g["target_eur"]) == ("2000", "2040", "2100")
    assert g["pct"] == "2" and g["target_pct"] == "5"
    assert g["status"]


async def test_read_only_and_never_mcp():
    from smtrading.mcp.server import build_mcp

    routes = set(_app_routes())
    assert ("GET", "/api/roadmap") in routes
    assert not [(m, p) for m, p in routes if p.startswith("/api/roadmap") and m != "GET"]
    names = [t.name for t in await build_mcp().list_tools()]
    assert not [n for n in names if "roadmap" in n]
