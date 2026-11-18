"""Hand-off watchdog (worker job): reads Paperclip issues (GET only, through a fake
client) and warns on Telegram, once per item and day, when a hand-off of HANDOFFS.md is
missing: AGENDA by 08:50, BUONGIORNO and its Telegram by 09:20 on market days, PROTEGGI /
RISCHIO OBIETTIVO without a reply after 45 minutes, ESEGUI still open after 30 minutes
with the market open."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.instruments.models import Instrument
from smtrading.paperclip.client import PaperclipClient, PaperclipError
from smtrading.paperclip.config import PaperclipConfig, save_config
from smtrading.paperclip.watchdog import ISSUES_LIMIT, STATE_KEY, HandoffWatchdog
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import AppSetting, RiskProfile
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

# Wednesday 18/11/2026 (Rome = UTC+1): every main exchange trades
DAY = "2026-11-18"
AT_0845 = datetime(2026, 11, 18, 7, 45, tzinfo=UTC)
AT_0855 = datetime(2026, 11, 18, 7, 55, tzinfo=UTC)
AT_0925 = datetime(2026, 11, 18, 8, 25, tzinfo=UTC)
PM_ID, NOT_ID, RM_ID, ES_ID = "pm-1", "not-1", "rm-1", "es-1"
ISSUES_CALL = f"issues:{ISSUES_LIMIT}"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _issue(n, title, *, assignee=PM_ID, status="todo", created=AT_0845):
    return {"id": f"uuid-{n}", "identifier": f"SMT-{n}", "title": title, "status": status,
            "assigneeAgentId": assignee,
            "createdAt": created.isoformat().replace("+00:00", "Z"),
            "updatedAt": created.isoformat()}


class FakePaperclip:
    """Stands for PaperclipClient (GET only): issues, comments per issue id, agents."""

    def __init__(self):
        self.issue_list: list[dict] = []
        self.comments: dict[str, list[dict]] = {}
        self.agents_list = [{"id": PM_ID, "name": "Portfolio Manager"},
                            {"id": NOT_ID, "name": "Notifier"},
                            {"id": RM_ID, "name": "Risk Manager"},
                            {"id": ES_ID, "name": "Executor"}]
        self.calls: list[str] = []
        self.fail: str | None = None

    def __call__(self, cfg):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def issues(self, limit):
        self.calls.append(f"issues:{limit}")
        if self.fail:
            raise PaperclipError(self.fail)
        return self.issue_list

    async def issue_comments(self, issue_id):
        self.calls.append(f"comments:{issue_id}")
        return self.comments.get(issue_id, [])

    async def agents(self):
        self.calls.append("agents")
        return self.agents_list


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
        for symbol, cls, market in (("AAPL", "stock", "NASDAQ"),
                                    ("BTC-USD", "crypto", "CRYPTO")):
            if await s.get(Instrument, symbol) is None:
                s.add(Instrument(symbol=symbol, name=symbol, asset_class=cls, market=market,
                                 currency="USD"))
        await s.commit()
    await _paperclip(sm)
    await _portfolio(sm)
    yield sm
    await engine.dispose()


async def _paperclip(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=enabled, agent_map={}, by="t")
        await s.commit()


async def _portfolio(sm, name="Aggressivo", markets=None) -> uuid.UUID:
    async with sm() as s:
        profile_id = None
        if markets is not None:
            profile = RiskProfile(name=f"p-{name}", allowed_markets=markets)
            s.add(profile)
            await s.flush()
            profile_id = profile.id
        p = await create_portfolio(s, name=name, initial_cash=D(10000),
                                   risk_profile_id=profile_id)
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _only_portfolio_markets(sm, markets):
    async with sm() as s:
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.commit()
    await _portfolio(sm, name=f"solo-{uuid.uuid4().hex[:6]}", markets=markets)


async def _outbox(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text)
                                    .where(TelegramOutbox.source == "system")
                                    .order_by(TelegramOutbox.id)))


async def _activity_count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(Activity))


async def _telegram(sm, text, at=AT_0845, source="agent:x"):
    async with sm() as s:
        s.add(TelegramOutbox(source=source, category="agent", level="info", text=text,
                             status="sent", created_at=at))
        await s.commit()


async def _setting(sm, key, value):
    async with sm() as s:
        await s.merge(AppSetting(key=key, value=value))
        await s.commit()


def _dog(sm, clock, fake):
    return HandoffWatchdog(sm, client_factory=fake, clock=clock, tz="Europe/Rome")


def _morning_ok(fake):
    fake.issue_list += [_issue(1, f"AGENDA {DAY}", status="done"),
                        _issue(2, f"BUONGIORNO {DAY}", assignee=NOT_ID, status="done")]


async def test_missing_agenda_warns_once_per_day_and_survives_restart(env):
    fake, clock = FakePaperclip(), Clock(AT_0845)
    activity = await _activity_count(env)
    await _dog(env, clock, fake).tick()
    assert await _outbox(env) == []  # before 08:50 nothing is due
    clock.t = AT_0855
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1
    assert texts[0].startswith("⚠ Passaggio mancante:") and f"AGENDA {DAY}" in texts[0]
    clock.t += timedelta(minutes=10)
    await _dog(env, clock, fake).tick()  # a new worker (restart): the dedup is persisted
    assert len(await _outbox(env)) == 1
    assert await _activity_count(env) == activity  # never the agents' feed


async def test_agenda_assigned_to_someone_else_is_a_miss(env):
    fake, clock = FakePaperclip(), Clock(AT_0855)
    fake.issue_list.append(_issue(1, f"AGENDA {DAY}", assignee=RM_ID))
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1 and "SMT-1" in texts[0] and "Portfolio Manager" in texts[0]


async def test_buongiorno_task_and_telegram_by_0920(env):
    fake, clock = FakePaperclip(), Clock(AT_0855)
    fake.issue_list.append(_issue(1, f"AGENDA {DAY}", status="done"))
    await _dog(env, clock, fake).tick()
    assert await _outbox(env) == []  # BUONGIORNO is due at 09:20
    clock.t = AT_0925
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1 and f"BUONGIORNO {DAY}" in texts[0]  # task missing: one warning

    fake.issue_list.append(_issue(2, f"BUONGIORNO {DAY}", assignee=NOT_ID))
    clock.t += timedelta(minutes=5)
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 2 and "Buongiorno – piano di oggi" in texts[1]


async def test_morning_chain_complete_sends_nothing(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    _morning_ok(fake)
    await _telegram(env, "Buongiorno – piano di oggi 18/11\nAGG: due ingressi")
    await _dog(env, clock, fake).tick()
    assert await _outbox(env) == []


async def test_yesterdays_buongiorno_telegram_does_not_count(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    _morning_ok(fake)
    await _telegram(env, "Buongiorno – piano di oggi 17/11", at=AT_0845 - timedelta(days=1))
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1 and "Buongiorno – piano di oggi" in texts[0]


async def test_no_morning_checks_on_holidays_weekends_or_crypto_only(env):
    fake = FakePaperclip()
    christmas = datetime(2026, 12, 25, 8, 30, tzinfo=UTC)  # every main exchange closed
    saturday = datetime(2026, 11, 21, 8, 30, tzinfo=UTC)
    for at in (christmas, saturday):
        await _dog(env, Clock(at), fake).tick()
    await _only_portfolio_markets(env, ["CRYPTO"])
    await _dog(env, Clock(AT_0925), fake).tick()
    assert await _outbox(env) == []


async def test_a_portfolio_market_open_makes_a_market_day(env):
    fake = FakePaperclip()
    eve = datetime(2026, 12, 24, 8, 30, tzinfo=UTC)  # MTA and XETRA closed, NYSE open
    await _only_portfolio_markets(env, ["MTA"])
    await _dog(env, Clock(eve), fake).tick()
    assert await _outbox(env) == []
    await _only_portfolio_markets(env, ["MTA", "NYSE"])
    await _dog(env, Clock(eve + timedelta(minutes=10)), fake).tick()
    texts = await _outbox(env)
    assert texts and all("2026-12-24" in t for t in texts)


async def test_protect_and_goal_risk_without_reply_after_45_minutes(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    _morning_ok(fake)
    await _telegram(env, "Buongiorno – piano di oggi 18/11")
    old, recent = AT_0925 - timedelta(minutes=50), AT_0925 - timedelta(minutes=20)
    fake.issue_list += [
        _issue(10, "PROTEGGI · Aggressivo · AAPL", assignee=RM_ID, created=old),
        _issue(11, "RISCHIO OBIETTIVO Mese · vicino al limite · 11/2026 · CEO",
               assignee="ceo", created=old),
        _issue(12, "PROTEGGI · Aggressivo · MSFT", assignee=RM_ID, created=recent),
        _issue(13, "PROTEGGI · Aggressivo · ENI", assignee=RM_ID, created=old,
               status="done"),
        _issue(14, "PROTEGGI · Aggressivo · TSLA", assignee=RM_ID, created=old),
    ]
    fake.comments["uuid-14"] = [{"id": "c1", "body": "ordini 123, 124"}]
    dog = _dog(env, clock, fake)
    await dog.tick()
    texts = await _outbox(env)
    assert len(texts) == 2
    assert any("SMT-10" in t and "50 min" in t for t in texts)
    assert any("SMT-11" in t for t in texts)
    # comments read only for the stale open candidates, never for recent or closed ones
    assert sorted(c for c in fake.calls if c.startswith("comments")) == [
        "comments:uuid-10", "comments:uuid-11", "comments:uuid-14"]

    fake.calls.clear()
    clock.t += timedelta(minutes=5)
    await dog.tick()
    assert len(await _outbox(env)) == 2  # once per item and day
    # warned and answered issues are not read again
    assert [c for c in fake.calls if c.startswith("comments")] == []


async def test_execute_open_after_30_minutes_with_the_market_open(env):
    fake = FakePaperclip()
    _morning_ok(fake)
    await _telegram(env, "Buongiorno – piano di oggi 18/11")
    at = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)  # NASDAQ open
    fake.issue_list += [
        _issue(20, "ESEGUI PROP-1 · AGG · AAPL · buy", assignee=ES_ID,
               created=at - timedelta(minutes=40)),
        _issue(21, "ESEGUI PROP-2 · AGG · AAPL · buy", assignee=ES_ID,
               created=at - timedelta(minutes=10)),
        _issue(22, "ESEGUI PROP-3 · AGG · AAPL · buy", assignee=ES_ID, status="done",
               created=at - timedelta(minutes=40)),
    ]
    await _dog(env, Clock(at), fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1 and "SMT-20" in texts[0] and "40 min" in texts[0]
    assert not [c for c in fake.calls if c.startswith("comments")]


async def test_execute_with_the_market_closed_waits(env):
    fake = FakePaperclip()
    _morning_ok(fake)
    await _telegram(env, "Buongiorno – piano di oggi 18/11")
    at = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)  # NASDAQ closed
    fake.issue_list += [
        _issue(20, "ESEGUI PROP-1 · AGG · AAPL · buy", assignee=ES_ID,
               created=at - timedelta(minutes=40)),
        _issue(21, "ESEGUI PROP-2 · CRY · BTC-USD · buy", assignee=ES_ID,
               created=at - timedelta(minutes=40)),
    ]
    await _dog(env, Clock(at), fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 1 and "SMT-21" in texts[0]  # crypto: always open


async def test_off_setting_or_paperclip_disabled_reads_nothing(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    await _setting(env, "handoff_watchdog", {"on": False})
    await _dog(env, clock, fake).tick()
    assert fake.calls == [] and await _outbox(env) == []
    await _setting(env, "handoff_watchdog", {"on": True})
    await _paperclip(env, enabled=False)
    await _dog(env, clock, fake).tick()
    assert fake.calls == [] and await _outbox(env) == []


async def test_at_most_one_pass_every_5_minutes_and_agents_cached(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    dog = _dog(env, clock, fake)
    await dog.tick()
    await dog.tick()
    clock.t += timedelta(minutes=2)
    await _dog(env, clock, fake).tick()  # a restart does not bypass the interval
    assert fake.calls.count(ISSUES_CALL) == 1
    clock.t += timedelta(minutes=4)
    await dog.tick()
    clock.t += timedelta(minutes=6)
    await dog.tick()
    assert fake.calls.count(ISSUES_CALL) == 3
    assert fake.calls.count("agents") == 1  # cached for an hour


async def test_paperclip_failure_sends_nothing_and_keeps_going(env):
    fake, clock = FakePaperclip(), Clock(AT_0925)
    fake.fail = "timeout"
    await _dog(env, clock, fake).tick()
    assert await _outbox(env) == []
    fake.fail = None
    clock.t += timedelta(minutes=5)
    await _dog(env, clock, fake).tick()
    assert len(await _outbox(env)) == 2  # AGENDA and BUONGIORNO missing


async def test_new_day_warns_again(env):
    fake, clock = FakePaperclip(), Clock(AT_0855)
    await _dog(env, clock, fake).tick()
    clock.t += timedelta(days=1)  # Thursday 19/11
    await _dog(env, clock, fake).tick()
    texts = await _outbox(env)
    assert len(texts) == 2 and "2026-11-19" in texts[1]
    async with env() as s:
        state = await s.scalar(select(AppSetting.value).where(AppSetting.key == STATE_KEY))
    assert state["day"] == "2026-11-19" and len(state["warned"]) == 1


async def test_client_reads_issues_and_comments_with_get_only():
    seen: list[tuple[str, str]] = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[{"id": "c1"}])
        return httpx.Response(200, json={"issues": [{"id": "i1", "title": "AGENDA"}]})

    cfg = PaperclipConfig(base_url="https://pc.example.test", company_id="c 1",
                          board_key="pcp_board_KEY", enabled=True)
    async with PaperclipClient(cfg, transport=httpx.MockTransport(handler)) as client:
        assert await client.issues(500) == [{"id": "i1", "title": "AGENDA"}]
        assert await client.issue_comments("i/1") == [{"id": "c1"}]
    assert seen == [("GET", "https://pc.example.test/api/companies/c%201/issues?limit=500"),
                    ("GET", "https://pc.example.test/api/issues/i%2F1/comments")]
