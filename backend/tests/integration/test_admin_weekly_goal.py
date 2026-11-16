"""`GET /api/weekly-goal`: the weekly goal of the home page, from the same maths as the
Telegram goal line (telegram/weekly_goal.py). Readable by the viewer."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import update
from test_admin_users import env  # noqa: F401

from smtrading.events import discard_post_commit
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import AppSetting, EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
MON, WED, FRI = 8, 10, 12  # March 2027


def rome(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2027, 3, day, h, m, tzinfo=ROME).astimezone(UTC)


async def _portfolio(sm, name: str):
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(0))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _snap(sm, pid, ts, equity: str, kind=SnapshotKind.periodic) -> None:
    async with sm() as s:
        s.add(EquitySnapshot(
            portfolio_id=pid, ts=ts, kind=kind, equity_eur=D(equity), cash_eur=D(equity),
            exposure_eur=D(0), margin_used_eur=D(0),
            local_date=ts.astimezone(ROME).date() if kind == SnapshotKind.day_open else None))
        await s.commit()


def _clock(monkeypatch, at: datetime) -> None:
    import smtrading.admin.routes_dashboard as routes

    monkeypatch.setattr(routes, "_now", lambda: at)


async def test_weekly_goal_total_and_per_portfolio(env, monkeypatch):
    _clock(monkeypatch, rome(FRI, 18))
    a = await _portfolio(env.sm, "Goal A")
    b = await _portfolio(env.sm, "Goal B")  # created on Wednesday: no Monday day_open
    e = await _portfolio(env.sm, "Goal E")  # archived: left out
    await _snap(env.sm, a, rome(1, 0, 1), "500", SnapshotKind.day_open)  # last week
    await _snap(env.sm, a, rome(MON, 0, 1), "600", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(MON, 12), "610")
    await _snap(env.sm, a, rome(FRI, 17), "603")
    await _snap(env.sm, b, rome(WED, 11), "400")
    await _snap(env.sm, b, rome(11, 15), "401.20")
    await _snap(env.sm, e, rome(MON, 0, 1), "9000", SnapshotKind.day_open)
    async with env.sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == e)
                        .values(status=PortfolioStatus.archived))
        await s.commit()

    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    r = await c.get("/api/weekly-goal")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["week_start"] == "2027-03-08"
    assert D(body["target_pct"]) == D(2)
    assert body["on"] is True
    t = body["total"]
    assert (D(t["start_eur"]), D(t["current_eur"]), D(t["target_eur"]), D(t["pct"])) == (
        D("1000"), D("1004.20"), D("1020"), D("0.42"))
    by_name = {p["name"]: p for p in body["portfolios"]}
    assert set(by_name) == {"Goal A", "Goal B"}
    pa, pb = by_name["Goal A"], by_name["Goal B"]
    assert pa["portfolio_id"] == str(a)
    assert (D(pa["start_eur"]), D(pa["current_eur"]), D(pa["pct"])) == (
        D(600), D(603), D("0.50"))
    assert (D(pb["start_eur"]), D(pb["current_eur"]), D(pb["pct"])) == (
        D(400), D("401.20"), D("0.30"))


async def test_weekly_goal_without_data_and_custom_pct(env, monkeypatch):
    _clock(monkeypatch, rome(FRI, 18))
    async with env.sm() as s:
        s.add(AppSetting(key="weekly_goal", value={"on": False, "pct": "1.5"}))
        await s.commit()
    email, _ = await env.add_user("admin")
    c = await env.client(email)
    body = (await c.get("/api/weekly-goal")).json()
    assert body["week_start"] == "2027-03-08"
    assert D(body["target_pct"]) == D("1.5") and body["on"] is False
    assert body["total"] == {"start_eur": None, "current_eur": None, "target_eur": None,
                             "pct": None}
    assert body["portfolios"] == []
