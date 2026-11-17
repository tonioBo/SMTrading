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

    await _snap(sm, pid, clock.t - timedelta(minutes=1), "1010")
    await _job(sm, clock, fake).tick()
    assert (await _outbox(sm))[1:] == ["🟢 Obiettivo settimana di nuovo in linea: +1,00% su +2%"]

    await _snap(sm, pid, clock.t + timedelta(minutes=4), "1001")  # behind again, same day
    clock.t += timedelta(minutes=5)
    await _job(sm, clock, fake).tick()
    assert len(await _outbox(sm)) == 2 and len(fake.created) == 1
    async with sm() as s:
        row = (await s.scalars(select(GoalAlert))).one()
        assert (row.status, row.alerted, str(row.period_start)) == (
            "in ritardo", None, "2027-03-08")
        assert await s.scalar(select(func.count()).select_from(Activity)) == activity


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
    await job.tick()
    assert fake.created == [] and fake.lookups == [title]
    async with sm() as s:
        row = (await s.scalars(select(GoalAlert))).one()
    assert row.issues["behind:CEO"]["ident"] == "SMT-9"
    assert len(await _outbox(sm)) == 1

    # a missing agent: tried ISSUE_ATTEMPTS times, then given up
    fake.agents_list = []
    await _goal(sm, name="rischio", kind="max_loss", target_pct=D(1), scope="portfolio",
                portfolio_id=pid)
    await _snap(sm, pid, rome(10, 11, 30), "980")
    for _ in range(ISSUE_ATTEMPTS + 2):
        await job.tick()
    async with sm() as s:
        rows = [r for r in await s.scalars(select(GoalAlert)) if r.status == "superato"]
    assert [e["attempts"] for e in rows[0].issues.values()] == [ISSUE_ATTEMPTS] * 2


class FakeRouter:
    def __init__(self):
        self.calls: list[str] = []

    async def get_quotes(self, symbols):
        self.calls.append(f"quotes {','.join(symbols)}")
        return {"SPY": Quote("SPY", None, None, D(110), NOW, "t"),
                "VWCE.DE": Quote("VWCE.DE", D(99), D(101), None, NOW, "t")}

    async def get_fx_rate(self, base, quote):
        self.calls.append(f"fx {base}{quote}")
        return D("0.9")

    async def get_bars(self, symbol, timeframe, start, end):
        self.calls.append(f"bars {symbol} {timeframe}")
        # Thursday 4 and Friday 5 March (US bars at 05:00 UTC, XETRA at 23:00 UTC before)
        hour = 5 if symbol == "SPY" else -1
        return [Bar(datetime(2027, 3, d, tzinfo=UTC) + timedelta(hours=hour), D(1), D(1),
                    D(1), D(c), D(1)) for d, c in ((4, 98), (5, 100))]


async def test_benchmark_sampler_backfills_and_samples(env):
    sm = env
    pid = await _portfolio(sm)
    async with sm() as s:
        for sym, ccy in (("SPY", "USD"), ("VWCE.DE", "EUR")):
            if await s.get(Instrument, sym) is None:
                s.add(Instrument(symbol=sym, name=sym, asset_class="us_equity",
                                 market="NYSE", currency=ccy))
        await s.execute(update(Instrument).where(Instrument.symbol == "SPY")
                        .values(currency="USD"))
        await s.commit()
    await _goal(sm, name="spy", kind="beat_index", benchmark="SPY", target_pct=D(0))
    await _goal(sm, name="vwce", kind="beat_index", benchmark="VWCE.DE", target_pct=D(0),
                recurring=False, start_date=datetime(2027, 3, 8).date())
    await _snap(sm, pid, rome(8, 0, 1), "1000", SnapshotKind.day_open)
    await _snap(sm, pid, rome(10, 11), "1080")
    clock, fake, router = Clock(NOW), FakeIssues(), FakeRouter()
    await _job(sm, clock, fake, router).tick()
    async with sm() as s:
        rows = [(r.symbol, r.ts.astimezone(ROME).strftime("%d %H:%M"), r.price_eur)
                for r in await s.scalars(select(BenchmarkSnapshot)
                                         .order_by(BenchmarkSnapshot.symbol, BenchmarkSnapshot.ts))]
    assert rows == [("SPY", "04 22:30", D("88.2")), ("SPY", "05 22:30", D(90)),
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
