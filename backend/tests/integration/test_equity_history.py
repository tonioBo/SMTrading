"""`GET /api/portfolios/equity-history?range=`: the «Andamento» chart of the dashboard and
of the portfolio detail, from the `equity_snapshots`. Readable by the viewer."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import update
from test_admin_users import env  # noqa: F401

from smtrading.events import discard_post_commit
from smtrading.goals.models import BenchmarkSnapshot, Goal
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind

D = Decimal
ROME = ZoneInfo("Europe/Rome")
MON, WED, FRI = 8, 10, 12  # March 2027
URL = "/api/portfolios/equity-history"


def rome(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2027, 3, day, h, m, tzinfo=ROME).astimezone(UTC)


NOW = rome(FRI, 18)


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


def _clock(monkeypatch, at: datetime = NOW) -> None:
    import smtrading.admin.routes_equity_history as routes

    monkeypatch.setattr(routes, "_now", lambda: at)


async def _archive(sm, pid) -> None:
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()


async def _get(env, role="viewer", **params):
    email, _ = await env.add_user(role)
    c = await env.client(email)
    r = await c.get(URL, params=params)
    return r


def _points(item) -> list[tuple[str, Decimal]]:
    return [(p["ts"], D(p["equity_eur"])) for p in item["series"]]


async def test_1d_intraday_points_of_live_portfolios(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Storia A")
    b = await _portfolio(env.sm, "Storia B")
    gone = await _portfolio(env.sm, "Storia archiviata")
    await _snap(env.sm, a, rome(WED, 12), "90")  # older than a day: out of 1d
    await _snap(env.sm, a, rome(FRI, 0, 1), "100", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(FRI, 9), "101")
    await _snap(env.sm, a, rome(FRI, 17, 55), "102.5")
    await _snap(env.sm, b, rome(FRI, 10), "400")
    await _snap(env.sm, gone, rome(FRI, 10), "999")
    await _archive(env.sm, gone)

    r = await _get(env, range="1d")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"] == "1d"
    assert body["goal_pct"] is None
    by_name = {p["name"]: p for p in body["portfolios"]}
    assert set(by_name) == {"Storia A", "Storia B"}
    pa = by_name["Storia A"]
    assert pa["id"] == str(a)
    assert [v for _, v in _points(pa)] == [D(100), D(101), D("102.50")]
    assert datetime.fromisoformat(pa["series"][0]["ts"]) == rome(FRI, 0, 1)
    assert (D(pa["first_eur"]), D(pa["last_eur"]), D(pa["change_eur"]), D(pa["change_pct"])) \
        == (D(100), D("102.5"), D("2.5"), D("2.50"))
    assert pa["goal"] is None
    assert [v for _, v in _points(by_name["Storia B"])] == [D(400)]


async def test_1w_intraday_with_the_weekly_goal(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Goal A")
    b = await _portfolio(env.sm, "Goal B")  # created on Wednesday: no Monday day_open
    await _snap(env.sm, a, rome(4, 12), "550")  # 8 days ago: out of 1w
    await _snap(env.sm, a, rome(MON, 0, 1), "600", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(MON, 12), "610")
    await _snap(env.sm, a, rome(FRI, 17), "603")
    await _snap(env.sm, b, rome(WED, 11), "400")

    body = (await _get(env, range="1w")).json()
    assert D(body["goal_pct"]) == D(2)
    by_name = {p["name"]: p for p in body["portfolios"]}
    pa, pb = by_name["Goal A"], by_name["Goal B"]
    assert [v for _, v in _points(pa)] == [D(600), D(610), D(603)]
    goal = pa["goal"]
    assert datetime.fromisoformat(goal["week_start"]) == rome(MON, 0)
    assert (D(goal["base_eur"]), D(goal["target_eur"])) == (D(600), D(612))
    assert (D(pb["goal"]["base_eur"]), D(pb["goal"]["target_eur"])) == (D(400), D(408))


async def test_1w_without_the_goal_when_it_is_off(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Goal off")
    await _snap(env.sm, a, rome(MON, 0, 1), "600", SnapshotKind.day_open)
    async with env.sm() as s:
        await s.execute(update(Goal).values(active=False, target_pct=D("1.5")))
        await s.commit()
    body = (await _get(env, range="1w")).json()
    assert body["goal_pct"] is None
    assert body["portfolios"][0]["goal"] is None


async def test_daily_ranges_use_day_opens_and_end_on_the_latest_value(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Giornaliero")
    old = NOW - timedelta(days=45)
    await _snap(env.sm, a, old, "80", SnapshotKind.day_open)  # out of 1m, in 3m
    await _snap(env.sm, a, old + timedelta(hours=3), "81")
    await _snap(env.sm, a, rome(WED, 0, 1), "100", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(WED, 12), "130")  # intraday: not a daily point
    await _snap(env.sm, a, rome(FRI, 0, 1), "104", SnapshotKind.day_open)
    await _snap(env.sm, a, rome(FRI, 17, 55), "106")  # the latest value closes the curve

    month = (await _get(env, range="1m")).json()["portfolios"][0]
    assert [v for _, v in _points(month)] == [D(100), D(104), D(106)]
    assert D(month["change_pct"]) == D("6.00")
    quarter = (await _get(env, range="3m")).json()["portfolios"][0]
    assert [v for _, v in _points(quarter)] == [D(80), D(100), D(104), D(106)]
    everything = (await _get(env, range="all")).json()["portfolios"][0]
    assert _points(everything) == _points(quarter)


async def test_daily_range_without_day_opens_falls_back_to_every_snapshot(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Nuovo")
    await _snap(env.sm, a, rome(FRI, 10), "50")
    await _snap(env.sm, a, rome(FRI, 11), "51")
    item = (await _get(env, range="1m")).json()["portfolios"][0]
    assert [v for _, v in _points(item)] == [D(50), D(51)]


async def test_one_portfolio_even_archived_and_empty_series(env, monkeypatch):
    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Uno")
    other = await _portfolio(env.sm, "Altro")
    await _snap(env.sm, a, rome(FRI, 10), "50")
    await _snap(env.sm, other, rome(FRI, 10), "70")
    await _archive(env.sm, a)
    body = (await _get(env, range="1d", portfolio_id=str(a))).json()
    assert [p["name"] for p in body["portfolios"]] == ["Uno"]
    assert [v for _, v in _points(body["portfolios"][0])] == [D(50)]

    empty = await _portfolio(env.sm, "Vuoto")
    item = (await _get(env, range="1d", portfolio_id=str(empty))).json()["portfolios"][0]
    assert item["series"] == []
    assert item["first_eur"] is None and item["change_pct"] is None


async def test_invalid_range_and_unknown_portfolio(env, monkeypatch):
    _clock(monkeypatch)
    r = await _get(env, range="2d")
    assert r.status_code == 400, r.text
    assert r.json()["details"]["field"] == "range"
    r = await _get(env, range="1d", portfolio_id="00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404, r.text
    # default range
    r = await _get(env)
    assert r.status_code == 200 and r.json()["range"] == "1m"


async def test_goal_lines_of_the_shown_scope(env, monkeypatch):
    """`goals`: the active goals whose current period overlaps the range, of the shown
    scope (all: total and portfolio goals; one portfolio: its goals only)."""
    from datetime import date

    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Linee A")
    b = await _portfolio(env.sm, "Linee B")
    await _snap(env.sm, a, rome(MON, 0, 1), "600", SnapshotKind.day_open)
    await _snap(env.sm, b, rome(WED, 11), "400")
    async with env.sm() as s:
        s.add(Goal(name="mese B", horizon="month", start_date=date(2027, 3, 1),
                   recurring=False, scope="portfolio", portfolio_id=b,
                   target_pct=D("4"), kind="min", active=True))
        s.add(Goal(name="anno prossimo", horizon="year", start_date=date(2028, 1, 1),
                   recurring=False, scope="total", target_pct=D("10"), kind="min",
                   active=True))  # its period starts after now: not drawn
        s.add(Goal(name="spento", horizon="week", start_date=date(2027, 3, 8),
                   recurring=False, scope="total", target_pct=D("1"), kind="min",
                   active=False))
        await s.commit()

    body = (await _get(env, range="1w")).json()
    lines = {g["name"]: g for g in body["goals"]}
    assert set(lines) == {"settimanale", "mese B"}
    week = lines["settimanale"]
    assert set(week) == {"goal_id", "name", "horizon", "scope", "portfolio_id", "kind",
                         "benchmark", "target_pct", "period_start", "period_end",
                         "base_eur", "target_eur"}
    assert body["benchmarks"] == {}
    assert datetime.fromisoformat(week["period_start"]) == rome(MON, 0)
    assert datetime.fromisoformat(week["period_end"]) == rome(15, 0)
    assert (D(week["base_eur"]), D(week["target_eur"])) == (D(1000), D(1020))
    month = lines["mese B"]
    assert (month["scope"], month["portfolio_id"]) == ("portfolio", str(b))
    assert (D(month["base_eur"]), D(month["target_eur"])) == (D(400), D(416))
    assert datetime.fromisoformat(month["period_start"]) == rome(1, 0)

    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    one = (await c.get(URL, params={"range": "1m", "portfolio_id": str(a)})).json()
    assert one["goals"] == []
    one = (await c.get(URL, params={"range": "1m", "portfolio_id": str(b)})).json()
    assert [g["name"] for g in one["goals"]] == ["mese B"]


async def test_benchmark_series_of_beat_index_goals(env, monkeypatch):
    """A beat_index goal carries kind and benchmark, its target from the benchmark's
    change; `benchmarks` holds the benchmark's EUR change from the period start over the
    range. A max_loss goal's target is the floor."""
    from datetime import date

    _clock(monkeypatch)
    a = await _portfolio(env.sm, "Indice A")
    await _snap(env.sm, a, rome(MON, 0, 1), "1000", SnapshotKind.day_open)
    async with env.sm() as s:
        s.add(Goal(name="indice", horizon="week", start_date=date(2027, 3, 8),
                   recurring=False, scope="portfolio", portfolio_id=a, target_pct=D("1"),
                   kind="beat_index", benchmark="SPY", active=True))
        s.add(Goal(name="rischio", horizon="month", start_date=date(2027, 3, 1),
                   recurring=False, scope="portfolio", portfolio_id=a, target_pct=D("5"),
                   kind="max_loss", active=True))
        for ts, eur in ((rome(5, 22, 30), "90"), (rome(WED, 15), "93.6"),
                        (rome(FRI, 17), "99"), (rome(FRI, 19), "120")):
            s.add(BenchmarkSnapshot(symbol="SPY", ts=ts, price=D(eur), currency="USD",
                                    fx_eur=D(1), price_eur=D(eur)))
        await s.commit()
    body = (await _get(env, range="1w")).json()
    lines = {g["name"]: g for g in body["goals"]}
    assert (lines["indice"]["kind"], lines["indice"]["benchmark"]) == ("beat_index", "SPY")
    assert D(lines["indice"]["target_eur"]) == D(1110)  # +10% of the index, +1 point
    assert (lines["rischio"]["benchmark"], D(lines["rischio"]["target_eur"])) == (None, D(950))
    assert [(datetime.fromisoformat(p["ts"]), D(p["pct"])) for p in body["benchmarks"]["SPY"]
            ] == [(rome(MON, 0), D(0)), (rome(WED, 15), D(4)), (rome(FRI, 17), D(10))]
    one = (await _get(env, range="1d", portfolio_id=str(a))).json()
    assert [D(p["pct"]) for p in one["benchmarks"]["SPY"]] == [D(10)]  # within the range
