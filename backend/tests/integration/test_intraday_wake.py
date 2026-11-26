"""Mode A wake (`intraday.wake.Waker`): one Paperclip issue for the portfolio's configured
agent, Telegram when no agent can be woken; hourly cap; one open issue per portfolio and
symbol. Paperclip is a fake (no network); agent and portfolio names are made up."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit
from smtrading.intraday.models import (
    CAPPED,
    FAILED,
    ISSUE_OPEN,
    LATE,
    NO_AGENT,
    PENDING,
    TELEGRAM,
    WOKEN,
    IntradayTrigger,
)
from smtrading.intraday.wake import Waker, wake_title
from smtrading.intraday.watch import Trigger
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import save_config
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

NOW = datetime(2026, 12, 1, 16, 1, tzinfo=UTC)
BAR = datetime(2026, 12, 1, 16, 0, tzinfo=UTC)
AGENT_NAME = "Agente Prova Xyz"
PC_ID = "pc-xyz-1"


class FakeIssues:
    def __init__(self):
        self.created: list[dict] = []
        self.fail: str | None = None
        self.timeout_on_create = False
        self.agents_list = [{"id": "other", "name": "Altro Nome"},
                            {"id": PC_ID, "name": AGENT_NAME}]
        self.open_ids: set[str] = set()
        self.existing: list[dict] = []
        self.lookups: list[str] = []
        self.configs = []

    def __call__(self, cfg):
        self.configs.append(cfg)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def agents(self):
        return self.agents_list

    async def open_identifiers(self):
        if self.fail:
            raise PaperclipError(self.fail)
        return set(self.open_ids)

    async def open_issue_with_title(self, title):
        self.lookups.append(title)
        return next((i["identifier"] for i in self.existing if i["title"] == title), None)

    async def create_issue(self, *, title, description, assignee_agent_id, priority="critical"):
        if self.fail:
            raise PaperclipError(self.fail)
        if self.timeout_on_create:
            self.existing.append({"identifier": "SMT-T", "title": title})
            raise PaperclipError("timeout")
        ident = f"SMT-{len(self.created) + 1}"
        self.created.append({"title": title, "description": description,
                             "assignee": assignee_agent_id, "priority": priority})
        self.open_ids.add(ident)
        return ident


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
        await s.commit()
    yield sm
    await engine.dispose()


async def _paperclip(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=enabled, agent_map={}, by="t")
        await s.commit()


async def _portfolio(sm, *, code="ZZT", access="trade", wake=True, active=True, cap=6,
                     agents_access=True, agent_name=AGENT_NAME) -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=f"Fittizio {uuid.uuid4().hex[:5]}",
                                   initial_cash=D(10000))
        p.code = code
        p.intraday_enabled = True
        p.intraday_max_wakes_per_hour = cap
        a = Agent(name=agent_name, token_prefix=uuid.uuid4().hex[:8], token_hash="x",
                  active=active)
        s.add(a)
        await s.flush()
        if agents_access:
            s.add(AgentPortfolio(agent_id=a.id, portfolio_id=p.id, permission=access))
        if wake:
            p.intraday_wake_agent_id = a.id
        discard_post_commit(s)
        await s.commit()
        return p.id


def _trig(pid, symbol="AAPL", *, rule="entry", late=False, ref="r1") -> Trigger:
    return Trigger(portfolio_id=pid, ref=ref, name="Strat Prova Q", symbol=symbol, rule=rule,
                   timeframe="15m", bar_close=BAR, value=D("28.5"), threshold=D(30),
                   price=D("123.4"), rule_text="rsi sotto 30", late=late)


def _waker(sm, fake):
    return Waker(sm, issues_factory=fake, clock=lambda: NOW, tz="Europe/Rome")


async def _rows(sm) -> list[IntradayTrigger]:
    async with sm() as s:
        return list(await s.scalars(select(IntradayTrigger).order_by(IntradayTrigger.id)))


async def _outbox(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def test_woken_creates_one_issue_for_the_configured_agent(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert len(fake.created) == 1
    c = fake.created[0]
    assert c["assignee"] == PC_ID and c["priority"] == "high"
    assert c["title"] == "SEGNALE · ZZT · AAPL · entrata"
    assert "Strat Prova Q" in c["description"]
    [row] = await _rows(env)
    assert (row.outcome, row.issue_ident, row.error) == (WOKEN, "SMT-1", None)
    assert await _outbox(env) == []


async def test_one_open_issue_per_portfolio_symbol(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    w = _waker(env, fake)
    await w.handle([_trig(pid)])
    await w.handle([_trig(pid, rule="exit")])
    assert len(fake.created) == 1
    _, r2 = await _rows(env)
    assert (r2.outcome, r2.issue_ident) == (ISSUE_OPEN, "SMT-1")
    fake.open_ids.clear()  # the issue is done
    await w.handle([_trig(pid)])
    assert len(fake.created) == 2
    assert (await _rows(env))[2].outcome == WOKEN


async def test_same_batch_same_symbol_one_issue(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid), _trig(pid, rule="exit", ref="r2")])
    assert len(fake.created) == 1
    assert [r.outcome for r in await _rows(env)] == [WOKEN, ISSUE_OPEN]


async def test_cap_per_hour(env):
    await _paperclip(env)
    pid = await _portfolio(env, cap=2)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid, s) for s in ("AAA", "BBB", "CCC")])
    assert len(fake.created) == 2
    assert [r.outcome for r in await _rows(env)] == [WOKEN, WOKEN, CAPPED]


async def test_cap_counts_only_the_last_hour(env):
    await _paperclip(env)
    pid = await _portfolio(env, cap=1)
    fake = FakeIssues()
    w = _waker(env, fake)
    await w.handle([_trig(pid, "AAA")])
    async with env() as s:
        await s.execute(update(IntradayTrigger).values(created_at=NOW - timedelta(seconds=3700)))
        await s.commit()
    await w.handle([_trig(pid, "BBB")])
    assert [r.outcome for r in await _rows(env)] == [WOKEN, WOKEN]


async def test_portfolio_without_agents_goes_to_telegram(env):
    await _paperclip(env)
    pid = await _portfolio(env, agents_access=False)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert fake.created == [] and fake.configs == []
    [row] = await _rows(env)
    assert row.outcome == TELEGRAM
    [msg] = await _outbox(env)
    assert (msg.source, msg.category) == ("system", "system")
    assert msg.text.startswith("SEGNALE · ZZT · AAPL · entrata")
    assert "Strat Prova Q" in msg.text


async def test_no_wake_agent_is_no_agent(env):
    await _paperclip(env)
    pid = await _portfolio(env, wake=False)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert fake.created == []
    [row] = await _rows(env)
    assert row.outcome == NO_AGENT
    [msg] = await _outbox(env)
    assert "nessun agente da svegliare: imposta il destinatario nel portafoglio" in msg.text
    assert "SEGNALE · ZZT · AAPL · entrata" in msg.text


async def test_agent_downgraded_to_read_is_no_agent(env):
    await _paperclip(env)
    pid = await _portfolio(env, access="read")
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert fake.created == []
    assert [r.outcome for r in await _rows(env)] == [NO_AGENT]
    assert len(await _outbox(env)) == 1


async def test_inactive_agent_is_no_agent(env):
    await _paperclip(env)
    pid = await _portfolio(env, active=False)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert [r.outcome for r in await _rows(env)] == [NO_AGENT]


async def test_paperclip_disabled_is_failed_with_telegram(env):
    await _paperclip(env, enabled=False)
    pid = await _portfolio(env)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    assert fake.created == []
    [row] = await _rows(env)
    assert row.outcome == FAILED and row.error
    [msg] = await _outbox(env)
    assert msg.text.startswith("SEGNALE · ZZT · AAPL · entrata")


async def test_paperclip_error_is_failed_with_short_error(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    fake.fail = "HTTP 500"
    await _waker(env, fake).handle([_trig(pid)])
    [row] = await _rows(env)
    assert (row.outcome, row.error) == (FAILED, "HTTP 500")
    assert len(await _outbox(env)) == 1


async def test_agent_missing_in_paperclip_is_failed(env):
    await _paperclip(env)
    pid = await _portfolio(env, agent_name="Senza Paperclip")
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid)])
    [row] = await _rows(env)
    assert row.outcome == FAILED and "Senza Paperclip" in row.error
    assert fake.created == [] and len(await _outbox(env)) == 1


async def test_timeout_adopts_existing_issue(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    fake.timeout_on_create = True
    await _waker(env, fake).handle([_trig(pid)])
    [row] = await _rows(env)
    assert (row.outcome, row.issue_ident) == (WOKEN, "SMT-T")
    assert fake.lookups == ["SEGNALE · ZZT · AAPL · entrata"]
    assert await _outbox(env) == []


async def test_stale_pending_adopted_not_duplicated(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    async with env() as s:
        p = await s.get(Portfolio, pid)
        t = _trig(pid)
        for sym in ("AAPL", "MSFT"):
            s.add(IntradayTrigger(
                created_at=NOW - timedelta(minutes=15), portfolio_id=pid, strategy_ref="r1",
                strategy_name="Strat Prova Q", symbol=sym, rule="entry", timeframe="15m",
                bar_close=BAR, value=t.value, threshold=t.threshold, price=t.price,
                rule_text=t.rule_text, outcome=PENDING))
        await s.commit()
    fake.existing = [{"identifier": "SMT-77", "title": wake_title(t, p)}]
    await _waker(env, fake).handle([])
    assert fake.created == []
    a, b = await _rows(env)
    assert (a.outcome, a.issue_ident) == (WOKEN, "SMT-77")
    assert b.outcome == FAILED


async def test_fresh_pending_is_left_alone(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    async with env() as s:
        s.add(IntradayTrigger(
            created_at=NOW - timedelta(minutes=2), portfolio_id=pid, strategy_ref="r1",
            strategy_name="S", symbol="AAPL", rule="entry", timeframe="15m", bar_close=BAR,
            rule_text="x", outcome=PENDING))
        await s.commit()
    await _waker(env, fake).handle([])
    assert [r.outcome for r in await _rows(env)] == [PENDING]
    assert fake.lookups == []


async def test_late_entry_recorded_without_wake(env):
    await _paperclip(env)
    pid = await _portfolio(env)
    fake = FakeIssues()
    await _waker(env, fake).handle([_trig(pid, late=True)])
    assert fake.created == [] and fake.configs == []
    assert [r.outcome for r in await _rows(env)] == [LATE]
    assert await _outbox(env) == []
