"""`/api/goals`: the goals with horizons and scopes (plan 14). Viewer reads, admin writes;
the progress from the equity snapshots (goals.progress)."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import update
from test_admin_users import env  # noqa: F401

from smtrading.events import discard_post_commit
from smtrading.goals.models import BenchmarkSnapshot
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
MON, WED, FRI = 8, 10, 12  # March 2027


def rome(day: int, h: int, m: int = 0, month: int = 3) -> datetime:
    return datetime(2027, month, day, h, m, tzinfo=ROME).astimezone(UTC)


async def _portfolio(sm, name: str):
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(0))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _archive(sm, pid) -> None:
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()


async def _snap(sm, pid, ts, equity: str, kind=SnapshotKind.periodic) -> None:
    async with sm() as s:
        s.add(EquitySnapshot(
            portfolio_id=pid, ts=ts, kind=kind, equity_eur=D(equity), cash_eur=D(equity),
            exposure_eur=D(0), margin_used_eur=D(0),
            local_date=ts.astimezone(ROME).date() if kind == SnapshotKind.day_open else None))
        await s.commit()


def _clock(monkeypatch, at: datetime) -> None:
    import smtrading.admin.routes_goals as routes

    monkeypatch.setattr(routes, "_now", lambda: at)


async def _admin(env):
    email, _ = await env.add_user("admin")
    return await env.client(email)


async def test_crud_and_validation(env, monkeypatch):
    _clock(monkeypatch, rome(WED, 10))
    c = await _admin(env)
    pid = await _portfolio(env.sm, "Obiettivi P")
    goals = (await c.get("/api/goals")).json()
    assert [(g["name"], g["horizon"], g["recurring"], g["scope"], g["target_pct"],
             g["kind"], g["active"], g["start_date"], g["end_date"]) for g in goals] == [
        ("settimanale", "week", True, "total", "2", "min", True, "2026-01-05", None)]

    r = await c.post("/api/goals", json={"name": "  Mese  di marzo ", "horizon": "month",
                                         "target_pct": "5.5", "scope": "portfolio",
                                         "portfolio_id": str(pid)})
    assert r.status_code == 200, r.text
    g = r.json()
    assert set(g) == {"id", "name", "horizon", "start_date", "end_date", "recurring",
                      "scope", "portfolio_id", "target_pct", "kind", "benchmark", "active",
                      "created_at", "updated_at", "portfolio_name", "portfolio_archived"}
    assert (g["name"], g["start_date"], g["end_date"], g["recurring"], g["portfolio_id"],
            g["portfolio_name"], g["target_pct"], g["kind"], g["active"]) == (
        "Mese di marzo", "2027-03-10", None, False, str(pid), "Obiettivi P", "5.5", "min",
        True)

    r = await c.patch(f"/api/goals/{g['id']}", json={"scope": "total", "kind": "cap",
                                                     "recurring": True})
    assert r.status_code == 200, r.text
    assert (r.json()["scope"], r.json()["portfolio_id"], r.json()["kind"],
            r.json()["recurring"]) == ("total", None, "cap", True)
    custom = (await c.post("/api/goals", json={
        "name": "Natale", "horizon": "custom", "start_date": "2027-12-20",
        "end_date": "2028-01-08", "target_pct": 3})).json()
    assert (custom["start_date"], custom["end_date"]) == ("2027-12-20", "2028-01-08")

    r = await c.delete(f"/api/goals/{g['id']}")
    assert r.status_code == 200 and r.json()["active"] is False
    listed = (await c.get("/api/goals")).json()
    assert [x["name"] for x in listed] == ["settimanale", "Natale", "Mese di marzo"]

    bad = [
        {"horizon": "week", "target_pct": "2"},  # no name
        {"name": "x", "horizon": "day", "target_pct": "2"},
        {"name": "x", "horizon": "week", "target_pct": "0"},
        {"name": "x", "horizon": "week", "target_pct": "1.234"},
        {"name": "x", "horizon": "week", "target_pct": "1001"},
        {"name": "x", "horizon": "custom", "target_pct": "2"},  # no end_date
        {"name": "x", "horizon": "custom", "target_pct": "2", "start_date": "2027-03-10",
         "end_date": "2027-03-01"},
        {"name": "x", "horizon": "custom", "target_pct": "2", "end_date": "2027-04-01",
         "recurring": True},
        {"name": "x", "horizon": "week", "target_pct": "2", "scope": "portfolio"},
        {"name": "x", "horizon": "week", "target_pct": "2", "kind": "max"},
        {"name": "x", "horizon": "week", "target_pct": "2", "start_date": "10/03/2027"},
        {"name": "x" * 81, "horizon": "week", "target_pct": "2"},
    ]
    for body in bad:
        r = await c.post("/api/goals", json=body)
        assert r.status_code == 400, (body, r.text)
    r = await c.post("/api/goals", json={"name": "x", "horizon": "week", "target_pct": "2",
                                         "scope": "portfolio",
                                         "portfolio_id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert (await c.patch(f"/api/goals/{uuid.uuid4()}", json={})).status_code == 404
    assert (await c.delete(f"/api/goals/{uuid.uuid4()}")).status_code == 404
    r = await c.patch(f"/api/goals/{custom['id']}", json={"end_date": "2027-01-01"})
    assert r.status_code == 400
    r = await c.patch(f"/api/goals/{custom['id']}", json={"name": None})
    assert r.status_code == 400

    await _archive(env.sm, pid)
    r = await c.post("/api/goals", json={"name": "x", "horizon": "week", "target_pct": "2",
                                         "scope": "portfolio", "portfolio_id": str(pid)})
    assert r.status_code == 400 and "archiviato" in r.json()["message"]


async def test_viewer_reads_only(env, monkeypatch):
    _clock(monkeypatch, rome(WED, 10))
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    assert (await c.get("/api/goals")).status_code == 200
    assert (await c.get("/api/goals/progress")).status_code == 200
    r = await c.post("/api/goals", json={"name": "x", "horizon": "week", "target_pct": "2"})
    assert r.status_code == 403


async def test_progress_total_portfolio_and_archived(env, monkeypatch):
    _clock(monkeypatch, rome(WED, 12))  # Wednesday noon: 50% of the week's business days
    c = await _admin(env)
    a = await _portfolio(env.sm, "Goal A")
    b = await _portfolio(env.sm, "Goal B")  # created on Tuesday: no Monday day_open
    e = await _portfolio(env.sm, "Goal E")  # archived: left out
    await _snap(env.sm, a, rome(1, 0, 1), "500", SnapshotKind.day_open)  # last week
    await _snap(env.sm, a, rome(MON, 0, 1), "600", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(MON, 12), "610")
    await _snap(env.sm, a, rome(WED, 11), "603")
    await _snap(env.sm, b, rome(9, 11), "400")
    await _snap(env.sm, b, rome(WED, 11, 30), "401.20")
    await _snap(env.sm, e, rome(MON, 0, 1), "9000", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(WED, 13), "999")  # after now: ignored
    for body in ({"name": "B mese", "horizon": "month", "target_pct": "1", "recurring": True,
                  "scope": "portfolio", "portfolio_id": str(b), "start_date": "2027-01-01"},
                 {"name": "E", "horizon": "week", "target_pct": "1", "scope": "portfolio",
                  "portfolio_id": str(e), "start_date": "2027-03-08"},
                 {"name": "Tetto", "horizon": "week", "target_pct": "0.1", "kind": "cap",
                  "start_date": "2027-03-08"},
                 {"name": "Aprile", "horizon": "month", "target_pct": "3",
                  "start_date": "2027-04-01"}):
        assert (await c.post("/api/goals", json=body)).status_code == 200
    await _archive(env.sm, e)

    rows = (await c.get("/api/goals/progress")).json()
    by = {p["name"]: p for p in rows}
    # by horizon, the total before the portfolios, then by name
    assert [p["name"] for p in rows] == ["settimanale", "Tetto", "Aprile", "B mese"]
    week = by["settimanale"]
    assert set(week) == {"goal_id", "name", "horizon", "scope", "portfolio_id",
                         "portfolio_name", "kind", "period_start", "period_end", "start_eur",
                         "current_eur", "target_eur", "pct", "target_pct", "gap_eur",
                         "elapsed_pct", "status", "as_of", "benchmark", "benchmark_pct",
                         "diff_pp", "loss_pct"}
    assert (week["period_start"], week["period_end"], week["scope"]) == (
        "2027-03-08", "2027-03-14", "total")
    assert (D(week["start_eur"]), D(week["current_eur"]), D(week["target_eur"]),
            D(week["pct"]), D(week["gap_eur"]), D(week["elapsed_pct"])) == (
        D(1000), D("1004.2"), D(1020), D("0.42"), D("15.8"), D(50))
    assert week["status"] == "in ritardo"  # 0,42 < 2 × 50% − 0,5 = 0,5
    assert datetime.fromisoformat(week["as_of"]) == rome(WED, 11, 30)
    assert by["Tetto"]["status"] == "oltre il tetto"
    month = by["B mese"]
    assert (month["portfolio_id"], month["portfolio_name"], month["period_start"],
            month["period_end"]) == (str(b), "Goal B", "2027-03-01", "2027-03-31")
    # no snapshot on 1 March: the first one after the start
    assert (D(month["start_eur"]), D(month["current_eur"]), D(month["pct"])) == (
        D(400), D("401.2"), D("0.3"))
    april = by["Aprile"]
    assert april["status"] == "non iniziato" and april["start_eur"] is None
    assert april["pct"] is None and april["as_of"] is None and D(april["elapsed_pct"]) == 0


async def test_weekly_goal_compat_follows_the_goal(env, monkeypatch):
    """`GET /api/weekly-goal` and the app setting read «the weekly goal»."""
    import smtrading.admin.routes_dashboard as dash

    monkeypatch.setattr(dash, "_now", lambda: rome(FRI, 18))
    c = await _admin(env)
    goal = (await c.get("/api/goals")).json()[0]
    r = await c.patch(f"/api/goals/{goal['id']}", json={"target_pct": "1.25"})
    assert r.status_code == 200
    body = (await c.get("/api/weekly-goal")).json()
    assert (body["on"], D(body["target_pct"])) == (True, D("1.25"))
    assert (await c.get("/api/settings/app")).json()["weekly_goal"] == {"on": True,
                                                                         "pct": "1.25"}
    await c.delete(f"/api/goals/{goal['id']}")
    assert (await c.get("/api/weekly-goal")).json()["on"] is False
    r = await c.put("/api/settings/app", json={"weekly_goal": {"on": True, "pct": "3"}})
    assert r.json()["weekly_goal"] == {"on": True, "pct": "3.0"}
    goals = (await c.get("/api/goals")).json()
    assert [(g["name"], g["active"], g["target_pct"]) for g in goals] == [
        ("settimanale", True, "3")]


async def test_weekly_setting_follows_the_earliest_active_weekly_goal(env):
    """With several recurring weekly minimum goals on the total, the setting's toggle and
    percent are the earliest active one (the earliest of all when none is active)."""
    c = await _admin(env)
    [first] = (await c.get("/api/goals")).json()
    r = await c.post("/api/goals", json={"name": "seconda", "horizon": "week",
                                         "recurring": True, "target_pct": "4"})
    assert r.status_code == 200, r.text
    second = r.json()
    assert (await c.patch(f"/api/goals/{first['id']}",
                          json={"active": False})).status_code == 200
    assert (await c.get("/api/settings/app")).json()["weekly_goal"] == {"on": True,
                                                                         "pct": "4.0"}
    r = await c.put("/api/settings/app", json={"weekly_goal": {"pct": "5"}})
    assert r.status_code == 200, r.text
    by = {g["id"]: g for g in (await c.get("/api/goals")).json()}
    assert (by[second["id"]]["target_pct"], by[second["id"]]["active"]) == ("5", True)
    assert (by[first["id"]]["target_pct"], by[first["id"]]["active"]) == ("2", False)
    # none active: the earliest one
    assert (await c.patch(f"/api/goals/{second['id']}",
                          json={"active": False})).status_code == 200
    assert (await c.get("/api/settings/app")).json()["weekly_goal"] == {"on": False,
                                                                         "pct": "2.0"}


async def test_weekly_setting_round_trips_the_goals_limit(env):
    """A weekly goal above 100% (the goals allow up to 1000%) reads and saves back."""
    c = await _admin(env)
    [goal] = (await c.get("/api/goals")).json()
    assert (await c.patch(f"/api/goals/{goal['id']}",
                          json={"target_pct": "250"})).status_code == 200
    current = (await c.get("/api/settings/app")).json()["weekly_goal"]
    assert current == {"on": True, "pct": "250.0"}
    r = await c.put("/api/settings/app", json={"weekly_goal": current})
    assert r.status_code == 200, r.text
    r = await c.put("/api/settings/app", json={"weekly_goal": {"pct": "1000"}})
    assert r.status_code == 200, r.text
    assert r.json()["weekly_goal"]["pct"] == "1000.0"
    r = await c.put("/api/settings/app", json={"weekly_goal": {"pct": "1000.01"}})
    assert r.status_code == 400, r.text


async def _bench(sm, ts, price_eur: str, symbol="VWCE.DE") -> None:
    async with sm() as s:
        s.add(BenchmarkSnapshot(symbol=symbol, ts=ts, price=D(price_eur), currency="EUR",
                                fx_eur=D(1), price_eur=D(price_eur)))
        await s.commit()


async def test_max_loss_and_beat_index_goals(env, monkeypatch):
    """max_loss: the loss limit and its statuses; beat_index: the scope's change against
    the benchmark's (EUR, from the benchmark snapshots), margin 0 allowed; validation."""
    _clock(monkeypatch, rome(WED, 12))
    c = await _admin(env)
    a = await _portfolio(env.sm, "Rischio A")
    await _snap(env.sm, a, rome(MON, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(WED, 11), "958")
    bad = [
        {"name": "x", "horizon": "week", "target_pct": "0", "kind": "max_loss"},
        {"name": "x", "horizon": "week", "target_pct": "0", "kind": "beat_index"},  # no index
        {"name": "x", "horizon": "week", "target_pct": "0", "kind": "beat_index",
         "benchmark": "DAX"},
        {"name": "x", "horizon": "week", "target_pct": "-1", "kind": "beat_index",
         "benchmark": "SPY"},
    ]
    for body in bad:
        r = await c.post("/api/goals", json=body)
        assert r.status_code == 400, (body, r.text)
    risk = (await c.post("/api/goals", json={
        "name": "rischio", "horizon": "week", "target_pct": "5", "kind": "max_loss",
        "scope": "portfolio", "portfolio_id": str(a), "start_date": "2027-03-08",
        "benchmark": "SPY"})).json()  # a benchmark of another kind is dropped
    assert (risk["kind"], risk["benchmark"], risk["target_pct"]) == ("max_loss", None, "5")
    beat = (await c.post("/api/goals", json={
        "name": "indice", "horizon": "week", "target_pct": "0", "kind": "beat_index",
        "benchmark": "vwce.de", "scope": "portfolio", "portfolio_id": str(a),
        "start_date": "2027-03-08"})).json()
    assert (beat["kind"], beat["benchmark"], beat["target_pct"]) == ("beat_index", "VWCE.DE", "0")

    by = {p["name"]: p for p in (await c.get("/api/goals/progress")).json()}
    r = by["rischio"]
    assert (D(r["pct"]), D(r["loss_pct"]), D(r["target_eur"]), D(r["gap_eur"]),
            r["status"], r["benchmark"], r["benchmark_pct"]) == (
        D("-4.2"), D("4.2"), D(950), D(-8), "vicino al limite", None, None)
    b = by["indice"]
    assert (b["status"], b["benchmark"], b["benchmark_pct"], b["diff_pp"],
            b["target_eur"]) == ("non iniziato", "VWCE.DE", None, None, None)

    await _bench(env.sm, rome(5, 22, 30), "100")  # Friday close: the base
    await _bench(env.sm, rome(MON, 15), "97")
    await _bench(env.sm, rome(WED, 11), "95")
    await _bench(env.sm, rome(WED, 13), "120")  # after now: ignored
    b = {p["name"]: p for p in (await c.get("/api/goals/progress")).json()}["indice"]
    assert (D(b["pct"]), D(b["benchmark_pct"]), D(b["diff_pp"]), D(b["target_eur"]),
            b["status"], b["loss_pct"]) == (D("-4.2"), D(-5), D("0.8"), D(950), "avanti", None)

    r = await c.patch(f"/api/goals/{beat['id']}", json={"target_pct": "2"})
    assert r.status_code == 200, r.text
    b = {p["name"]: p for p in (await c.get("/api/goals/progress")).json()}["indice"]
    assert b["status"] == "indietro"  # 0,8 < 2 - 0,5
    r = await c.patch(f"/api/goals/{beat['id']}", json={"kind": "min"})
    assert (r.json()["kind"], r.json()["benchmark"]) == ("min", None)
    r = await c.patch(f"/api/goals/{risk['id']}", json={"kind": "beat_index"})
    assert r.status_code == 400 and r.json()["details"]["field"] == "benchmark"
