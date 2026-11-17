"""Goal status-change alerts (worker job, plan 14 Task D): Telegram once per transition,
persisted per goal and period, Paperclip «OBIETTIVO» / «RISCHIO OBIETTIVO» issues with
dedup and timeout adoption, nothing in the agents' activity feed; the benchmark sampler
(backfill from daily bars, FX through the router). Paperclip and the router are fakes."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.goals.alerts import ISSUE_ATTEMPTS, GoalAlerts
from smtrading.goals.models import BenchmarkSnapshot, Goal, GoalAlert
from smtrading.goals.progress import active_progress
from smtrading.instruments.models import Instrument
from smtrading.marketdata.types import Bar, Quote
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import save_config
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import EquitySnapshot, SnapshotKind
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

ROME = ZoneInfo("Europe/Rome")


def rome(day: int, h: int, m: int = 0) -> datetime:
    return datetime(2027, 3, day, h, m, tzinfo=ROME).astimezone(UTC)


NOW = rome(10, 12)  # Wednesday noon: 50% of the week's business days


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeIssues:
    def __init__(self):
        self.created: list[dict] = []
        self.fail: str | None = None
        self.agents_list = [{"id": "ceo-1", "name": "CEO SMTrading"},
                            {"id": "rm-1", "name": "Risk Manager"}]
        self.existing: list[dict] = []
        self.lookups: list[str] = []

    def __call__(self, cfg):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def agents(self):
        return self.agents_list

    async def open_issue_with_title(self, title):
        self.lookups.append(title)
        return next((i["identifier"] for i in self.existing if i["title"] == title), None)

    async def create_issue(self, *, title, description, assignee_agent_id, priority="critical"):
        if self.fail:
            raise PaperclipError(self.fail)
        self.created.append({"title": title, "description": description,
                             "assignee": assignee_agent_id, "priority": priority})
        return f"SMT-{len(self.created)}"


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.execute(update(Goal).values(active=False))  # the migrated weekly goal
        await s.commit()
    yield sm
    await engine.dispose()


async def _paperclip(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=enabled, agent_map={}, by="t")
        await s.commit()


async def _portfolio(sm, name="Prudente") -> uuid.UUID:
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


async def _goal(sm, **kw) -> uuid.UUID:
    values = {"name": "settimana", "horizon": "week", "recurring": True, "scope": "total",
              "kind": "min", "target_pct": D(2), "active": True,
              "start_date": datetime(2027, 3, 1).date(), **kw}
    async with sm() as s:
        g = Goal(**values)
        s.add(g)
        await s.commit()
        return g.id


async def _outbox(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text).order_by(TelegramOutbox.id)))


def _job(sm, clock, fake, router=None):
    return GoalAlerts(sm, router, issues_factory=fake, clock=clock, tz="Europe/Rome")


async def test_behind_alerts_once_recovers_and_survives_restart(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _goal(sm)
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1002")  # +0,2% < 2 × 50% − 0,5
    clock, fake = Clock(NOW), FakeIssues()
    async with sm() as s:
        activity = await s.scalar(select(func.count()).select_from(Activity))
    await _job(sm, clock, fake).tick()
    assert await _outbox(sm) == [
        "🟠 Obiettivo settimana in ritardo: +0,20% su +2% (tempo trascorso 50%)"]
    assert [(i["title"], i["assignee"], i["priority"]) for i in fake.created] == [
        ("OBIETTIVO settimana · in ritardo · 08/03–14/03/2027", "ceo-1", "high")]
    assert "get_goals" in fake.created[0]["description"]

    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()  # a restart: nothing again
    assert len(await _outbox(sm)) == 1 and len(fake.created) == 1

    await _snap(sm, pid, clock.t - timedelta(minutes=1), "1006")  # in line, not past 0,25
    await _job(sm, clock, fake).tick()
    assert len(await _outbox(sm)) == 1

    await _snap(sm, pid, clock.t - timedelta(seconds=30), "1010")
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[1:] == ["🟢 Obiettivo settimana di nuovo in linea: +1,00% su +2%"]

    # behind again the same day: sent (the last message must not say «in linea»), and
    # the issue of the period is not opened twice
    await _snap(sm, pid, clock.t + timedelta(minutes=4), "1001")
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[2:] == [
        "🟠 Obiettivo settimana in ritardo: +0,10% su +2% (tempo trascorso 50,14%)"]
    assert len(fake.created) == 1
    # a second recovery the same day is held: the warning stays the last message
    await _snap(sm, pid, clock.t + timedelta(minutes=4), "1010")
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    assert len(await _outbox(sm)) == 3
    async with sm() as s:
        row = (await s.scalars(select(GoalAlert))).one()
        assert (row.status, row.alerted, str(row.period_start)) == (
            "in linea", "in ritardo", "2027-03-08")
        assert await s.scalar(select(func.count()).select_from(Activity)) == activity
    # the next day it is sent; reaching the goal reads «raggiunto»
    clock.t = rome(11, 9)
    await _snap(sm, pid, clock.t - timedelta(minutes=1), "1015")
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[3].startswith("🟢 Obiettivo settimana di nuovo in linea: +1,50%")
    await _snap(sm, pid, clock.t + timedelta(minutes=1), "990")
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    await _snap(sm, pid, clock.t + timedelta(minutes=1), "1021")
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[5:] == ["🎉 Obiettivo settimana raggiunto: +2,10% su +2%"]


async def test_changed_target_starts_the_alerts_over(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    gid = await _goal(sm)
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1002")
    clock, fake = Clock(NOW), FakeIssues()
    await _job(sm, clock, fake).tick()
    async with sm() as s:
        await s.execute(update(Goal).where(Goal.id == gid).values(target_pct=D(3)))
        await s.commit()
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[1:] == [
        "🟠 Obiettivo settimana in ritardo: +0,20% su +3% (tempo trascorso 50,07%)"]
    assert len(fake.created) == 2
    async with sm() as s:
        row = (await s.scalars(select(GoalAlert))).one()
    assert (row.kind, row.target_pct, row.alerted) == ("min", D(3), "in ritardo")


async def test_max_loss_near_breached_issues_and_no_paperclip(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _goal(sm, name="rischio", kind="max_loss", target_pct=D(5), scope="portfolio",
                portfolio_id=pid, horizon="month", start_date=datetime(2027, 3, 1).date())
    await _snap(sm, pid, rome(1, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "958")  # −4,2%: near (≥ 4%)
    clock, fake = Clock(NOW), FakeIssues()
    await _job(sm, clock, fake).tick()
    assert await _outbox(sm) == [
        "🟠 Obiettivo rischio · Prudente vicino al limite: perdita 4,20% su un limite del 5%"]
    assert [(i["title"], i["assignee"], i["priority"]) for i in fake.created] == [
        (("RISCHIO OBIETTIVO rischio · Prudente · vicino al limite · 01/03–31/03/2027 · "
         "Risk Manager"), "rm-1", "high"),
        ("RISCHIO OBIETTIVO rischio · Prudente · vicino al limite · 01/03–31/03/2027 · CEO",
         "ceo-1", "high")]
    assert "Nessun blocco automatico" in fake.created[0]["description"]

    await _snap(sm, pid, rome(10, 11, 30), "949")  # −5,1%: breached
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[1:] == [
        ("🔴 Obiettivo rischio · Prudente limite superato: perdita 5,10% su un limite del 5% "
        "(nessun blocco automatico)")]
    assert [(i["assignee"], i["priority"]) for i in fake.created[2:]] == [
        ("rm-1", "critical"), ("ceo-1", "critical")]

    await _snap(sm, pid, rome(10, 11, 35), "953")  # −4,7%: not 0,5 pp under the limit
    await _job(sm, clock, fake).tick()
    assert len(await _outbox(sm)) == 2

    await _snap(sm, pid, rome(10, 11, 40), "970")  # back near: recovered, no second near
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[2:] == [
        "🟢 Obiettivo rischio · Prudente di nuovo nel limite: perdita 3,00% su un limite del 5%"]
    assert len(fake.created) == 4

    # Paperclip off: Telegram only, no issue kept for later
    await _paperclip(sm, enabled=False)
    await _goal(sm, name="tetto", kind="cap", target_pct=D("0.5"), scope="portfolio",
                portfolio_id=pid, horizon="month")
    await _goal(sm, name="lento", scope="portfolio", portfolio_id=pid, horizon="month")
    await _job(sm, clock, fake).tick()
    texts = (await _outbox(sm))[3:]  # the cap goal at −3% is in line
    assert len(texts) == 1 and texts[0].startswith(
        "🟠 Obiettivo lento · Prudente in ritardo: -3,00% su +2% (tempo trascorso ")
    async with sm() as s:
        rows = {r.status: r.issues for r in await s.scalars(select(GoalAlert))}
    assert rows["in ritardo"] == {}
    assert len(fake.created) == 4


async def test_cap_above_and_back_under(env):
    sm = env
    pid = await _portfolio(sm)
    await _goal(sm, name="tetto", kind="cap", target_pct=D(1))
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1015")
    clock, fake = Clock(NOW), FakeIssues()
    await _job(sm, clock, fake).tick()
    await _snap(sm, pid, rome(10, 11, 30), "1005")
    await _job(sm, clock, fake).tick()
    assert await _outbox(sm) == [
        "🔴 Obiettivo tetto oltre il tetto: +1,50% su un tetto di +1%",
        "🟢 Obiettivo tetto di nuovo sotto il tetto: +0,50% su un tetto di +1%"]
    assert fake.created == []


async def test_failed_issue_retried_timeout_adopted_and_given_up(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _goal(sm)
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1000")
    clock, fake = Clock(NOW), FakeIssues()
    fake.fail = "timeout"
    job = _job(sm, clock, fake)
    await job.tick()
    assert fake.created == [] and fake.lookups == []
    fake.fail = None
    title = "OBIETTIVO settimana · in ritardo · 08/03–14/03/2027"
    fake.existing = [{"title": title, "identifier": "SMT-9"}]
    await job.tick()  # within the backoff: not tried
    assert fake.lookups == []
    clock.t += timedelta(minutes=5)
    await job.tick()
    assert fake.created == [] and fake.lookups == [title]
    async with sm() as s:
        row = (await s.scalars(select(GoalAlert))).one()
    assert row.issues["behind:CEO"]["ident"] == "SMT-9"
    assert row.issues["behind:CEO"]["attempts"] == 0
    assert len(await _outbox(sm)) == 1

    # a missing agent or an outage counts no attempt (tried again with a backoff)
    fake.agents_list = []
    await _goal(sm, name="rischio", kind="max_loss", target_pct=D(1), scope="portfolio",
                portfolio_id=pid)
    await _snap(sm, pid, rome(10, 11, 30), "980")
    for _ in range(ISSUE_ATTEMPTS + 2):
        clock.t += timedelta(hours=1)
        await job.tick()
    fake.agents_list = FakeIssues().agents_list
    fake.fail = "HTTP 503"
    clock.t += timedelta(hours=1)
    await job.tick()

    async def entries():
        async with sm() as s:
            rows = [r for r in await s.scalars(select(GoalAlert)) if r.status == "superato"]
        return list(rows[0].issues.values())

    assert [(e["attempts"], e["retries"]) for e in await entries()] == [
        (0, ISSUE_ATTEMPTS + 3)] * 2
    # a refused answer (4xx) counts: given up after ISSUE_ATTEMPTS
    fake.fail = "HTTP 422"
    for _ in range(ISSUE_ATTEMPTS + 2):
        clock.t += timedelta(hours=1)
        await job.tick()
    assert [e["attempts"] for e in await entries()] == [ISSUE_ATTEMPTS] * 2
    assert fake.created == []


class FakeRouter:
    def __init__(self):
        self.calls: list[str] = []
        self.fail: set[str] = set()  # "bars", "quotes SPY", "fx_on 2027-03-05", ...
        self.rates = {}  # day -> dated FX (default 0.9)

    def _check(self, call):
        self.calls.append(call)
        if call in self.fail or call.split(" ")[0] in self.fail:
            raise RuntimeError(call)

    async def get_quotes(self, symbols):
        self._check(f"quotes {','.join(symbols)}")
        quotes = {"SPY": Quote("SPY", None, None, D(110), NOW, "t"),
                  "VWCE.DE": Quote("VWCE.DE", D(99), D(101), None, NOW, "t")}
        return {s: quotes[s] for s in symbols if s in quotes}

    async def get_fx_rate(self, base, quote):
        self._check(f"fx {base}{quote}")
        return D("0.9")

    async def get_fx_rate_on(self, base, quote, day):
        self._check(f"fx_on {day.isoformat()}")
        return self.rates.get(day, D("0.9"))

    async def get_bars(self, symbol, timeframe, start, end):
        self._check(f"bars {symbol} {timeframe}")
        # Thursday 4 and Friday 5 March (US bars at 05:00 UTC, XETRA at 23:00 UTC before)
        hour = 5 if symbol == "SPY" else -1
        return [Bar(datetime(2027, 3, d, tzinfo=UTC) + timedelta(hours=hour), D(1), D(1),
                    D(1), D(c), D(1)) for d, c in ((4, 98), (5, 100))]


async def _instruments(sm):
    async with sm() as s:
        for sym, ccy in (("SPY", "USD"), ("VWCE.DE", "EUR")):
            if await s.get(Instrument, sym) is None:
                s.add(Instrument(symbol=sym, name=sym, asset_class="us_equity",
                                 market="NYSE", currency=ccy))
        await s.execute(update(Instrument).where(Instrument.symbol == "SPY")
                        .values(currency="USD"))
        await s.commit()


async def _bench_rows(sm, symbol=None):
    stmt = select(BenchmarkSnapshot).order_by(BenchmarkSnapshot.symbol, BenchmarkSnapshot.ts)
    if symbol:
        stmt = stmt.where(BenchmarkSnapshot.symbol == symbol)
    async with sm() as s:
        return [(r.symbol, r.ts.astimezone(ROME).strftime("%d %H:%M"), r.price_eur)
                for r in await s.scalars(stmt)]


async def test_benchmark_sampler_backfills_and_samples(env):
    sm = env
    pid = await _portfolio(sm)
    await _instruments(sm)
    await _goal(sm, name="spy", kind="beat_index", benchmark="SPY", target_pct=D(0))
    await _goal(sm, name="vwce", kind="beat_index", benchmark="VWCE.DE", target_pct=D(0),
                recurring=False, start_date=datetime(2027, 3, 8).date())
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1080")
    clock, fake, router = Clock(NOW), FakeIssues(), FakeRouter()
    router.rates = {datetime(2027, 3, 4).date(): D("0.8")}  # each close at its day's FX
    await _job(sm, clock, fake, router).tick()
    assert "quotes SPY" in router.calls and "quotes VWCE.DE" in router.calls
    rows = await _bench_rows(sm)
    assert rows == [("SPY", "04 22:30", D("78.4")), ("SPY", "05 22:30", D(90)),
                    ("SPY", "10 12:00", D(99)),
                    ("VWCE.DE", "04 22:30", D(98)), ("VWCE.DE", "05 22:30", D(100)),
                    ("VWCE.DE", "10 12:00", D(100))]
    async with sm() as s:
        by = {p["name"]: p for p in await active_progress(s, NOW, ROME)}
    assert (by["spy"]["benchmark_pct"], by["spy"]["diff_pp"], by["spy"]["status"]) == (
        D(10), D(-2), "indietro")
    assert (by["vwce"]["benchmark_pct"], by["vwce"]["status"]) == (D(0), "avanti")
    assert by["spy"]["pct"] == D(8)

    router.calls.clear()
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake, router).tick()  # no backfill again; same price: no row
    assert not any(c.startswith("bars") for c in router.calls)
    async with sm() as s:
        assert await s.scalar(select(func.count()).select_from(BenchmarkSnapshot)) == 6
    assert await _outbox(sm) == []  # beat_index sends no alert


async def test_benchmark_backfill_retried_with_backoff_and_no_late_base(env):
    sm = env
    pid = await _portfolio(sm)
    await _instruments(sm)
    await _goal(sm, name="spy", kind="beat_index", benchmark="SPY", target_pct=D(0))
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1080")
    clock, fake, router = Clock(NOW), FakeIssues(), FakeRouter()
    router.fail = {"bars", "quotes VWCE.DE"}
    job = _job(sm, clock, fake, router)
    await job.tick()
    # only the sample of Wednesday: more than a day after the start, no base
    assert await _bench_rows(sm) == [("SPY", "10 12:00", D(99))]
    async with sm() as s:
        (p,) = await active_progress(s, NOW, ROME)
    assert (p["benchmark_pct"], p["status"]) == (None, "non iniziato")

    router.fail = set()
    router.calls.clear()
    clock.t += timedelta(minutes=4)  # within the first backoff (5 minutes)
    await job.tick()
    assert not any(c.startswith("bars") for c in router.calls)
    clock.t += timedelta(minutes=2)
    await job.tick()
    assert [c for c in router.calls if c.startswith("bars")] == ["bars SPY 1d"]
    async with sm() as s:
        (p,) = await active_progress(s, clock.t, ROME)
    assert (p["benchmark_pct"], p["status"]) == (D(10), "indietro")


async def test_benchmark_day_without_fx_left_out_and_retried(env):
    sm = env
    await _instruments(sm)
    await _goal(sm, name="spy", kind="beat_index", benchmark="SPY", target_pct=D(0))
    clock, fake, router = Clock(NOW), FakeIssues(), FakeRouter()
    router.fail = {"fx_on 2027-03-05"}
    job = _job(sm, clock, fake, router)
    await job.tick()
    assert await _bench_rows(sm) == [("SPY", "04 22:30", D("88.2")),
                                     ("SPY", "10 12:00", D(99))]
    router.fail = set()
    clock.t += timedelta(minutes=6)
    await job.tick()
    assert await _bench_rows(sm) == [("SPY", "04 22:30", D("88.2")),
                                     ("SPY", "05 22:30", D(90)),
                                     ("SPY", "10 12:00", D(99))]
    router.calls.clear()
    clock.t += timedelta(hours=2)
    await job.tick()  # done: never again
    assert not any(c.startswith("bars") for c in router.calls)


async def test_benchmark_stale_current_row_gives_no_change(env):
    from smtrading.goals.benchmark import benchmark_change
    from smtrading.goals.periods import Period

    sm = env
    async with sm() as s:
        for day, h, eur in ((5, 22, "100"), (8, 22, "105")):  # Friday, Monday
            s.add(BenchmarkSnapshot(symbol="SPY", ts=rome(day, h), price=D(eur),
                                    currency="EUR", fx_eur=D(1), price_eur=D(eur)))
        await s.commit()
        month = Period(datetime(2027, 3, 5).date(), datetime(2027, 3, 31).date())
        week = Period(datetime(2027, 3, 8).date(), datetime(2027, 3, 14).date())
        assert await benchmark_change(s, "SPY", week, rome(9, 12), ROME) == D(5)
        assert await benchmark_change(s, "SPY", week, rome(9, 23), ROME) is None  # Tue night
        assert await benchmark_change(s, "SPY", week, rome(13, 12), ROME) is None
        # Friday's close read on Sunday or Monday morning is still fresh
        assert await benchmark_change(s, "SPY", month, rome(7, 12), ROME) == D(0)
        assert await benchmark_change(s, "SPY", month, rome(8, 9), ROME) == D(0)
        # no row before a period, and its first one more than a day after the start
        late = Period(datetime(2027, 3, 4).date(), datetime(2027, 3, 31).date())
        assert await benchmark_change(s, "SPY", late, rome(8, 9), ROME) is None


def test_reached_reads_raggiunto():
    from smtrading.goals.alerts import BACK_EV, alert_text

    p = {"name": "x", "portfolio_name": None, "target_pct": D(2), "pct": D("2.1"),
         "status": "raggiunto"}
    assert alert_text(BACK_EV, p) == "🎉 Obiettivo x raggiunto: +2,10% su +2%"
    assert alert_text(BACK_EV, {**p, "status": "in linea"}).startswith("🟢 Obiettivo x di")


def test_texts_are_agent_safe():
    from smtrading.common.fingerprints import FINGERPRINT
    from smtrading.goals.alerts import BEHIND_EV, BREACHED_EV, NEAR_EV, alert_text, issue_for

    progress = {"name": "x", "portfolio_name": None, "target_pct": D(2), "pct": D(-1),
                "elapsed_pct": D(50), "loss_pct": D(1), "status": "in ritardo",
                "period_start": datetime(2027, 3, 8).date(),
                "period_end": datetime(2027, 3, 14).date(),
                "start_eur": D(1000), "current_eur": D(990)}
    for event in (BEHIND_EV, NEAR_EV, BREACHED_EV):
        issue = issue_for(event, progress, "CEO")
        for text in (alert_text(event, progress), issue["title"], issue["description"]):
            assert not FINGERPRINT.search(text), text
