"""Protection alarm (worker job): Telegram warning and Paperclip «PROTEGGI» issue once per
position and problem, dedup persisted across restarts, cleared on resolution, nothing in
the agents' activity feed. Paperclip is a fake (no network)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.events import discard_post_commit
from smtrading.paperclip.client import PaperclipError
from smtrading.paperclip.config import save_config
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.positions.protection import ISSUE_ATTEMPTS, ProtectionAlarm, load_state
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

# Monday 16/11/2026, NASDAQ 14:30-21:00 UTC
NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)
CLOSE = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
OPENED = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeIssues:
    """Stands for PaperclipIssues: records the issues, fails on demand."""

    def __init__(self):
        self.created: list[dict] = []
        self.fail: str | None = None
        self.agents_list = [{"id": "ceo", "name": "CEO"}, {"id": "rm-1", "name": "Risk Manager"}]
        self.configs = []
        self.existing: list[dict] = []  # issues already in Paperclip
        self.lookups: list[str] = []

    def __call__(self, cfg):
        self.configs.append(cfg)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def agents(self):
        return self.agents_list

    async def open_issue_with_title(self, title):
        self.lookups.append(title)
        if self.fail:
            raise PaperclipError(self.fail)
        return next((i["identifier"] for i in self.existing if i["title"] == title), None)

    async def create_issue(self, *, title, description, assignee_agent_id, priority="critical"):
        if self.fail:
            raise PaperclipError(self.fail)
        self.created.append({"title": title, "description": description,
                             "assignee": assignee_agent_id})
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
        await s.commit()
    yield sm
    await engine.dispose()


async def _paperclip(sm, enabled=True):
    async with sm() as s:
        await save_config(s, base_url="https://pc.example.test", company_id="c1",
                          board_key="pcp_board_KEY", enabled=enabled, agent_map={}, by="t")
        await s.commit()


async def _portfolio(sm, name="Aggressivo", status=PortfolioStatus.active) -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(10000))
        p.status = status
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _position(sm, pid, symbol="AAPL", qty="10", *, opened=OPENED, updated=None):
    async with sm() as s:
        s.add(Position(portfolio_id=pid, symbol=symbol, qty=D(qty), avg_price=D(180),
                       opened_at=opened, updated_at=updated or opened))
        await s.commit()


async def _order(sm, pid, *, symbol="AAPL", side=OrderSide.sell, type=OrderType.stop,
                 tif=TimeInForce.gtc, qty="10", status=OrderStatus.pending, **kw) -> uuid.UUID:
    async with sm() as s:
        o = Order(portfolio_id=pid, symbol=symbol, side=side, type=type, tif=tif, qty=D(qty),
                  stop_price=D(170) if type != OrderType.limit else None,
                  limit_price=D(200) if type == OrderType.limit else None,
                  status=status, reduce_only=True, source="agent", **kw)
        s.add(o)
        await s.commit()
        return o.id


async def _set_order(sm, oid, **values):
    async with sm() as s:
        await s.execute(update(Order).where(Order.id == oid).values(**values))
        await s.commit()


async def _outbox(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text).order_by(TelegramOutbox.id)))


async def _activity_count(sm) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(Activity))


def _alarm(sm, clock, fake):
    return ProtectionAlarm(sm, issues_factory=fake, clock=clock, tz="Europe/Rome")


async def test_unprotected_position_alerts_once_and_survives_restart(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    activity_before = await _activity_count(sm)
    clock, fake = Clock(NOW), FakeIssues()
    await _alarm(sm, clock, fake).tick()
    assert await _outbox(sm) == [
        "⚠️ Posizione senza protezione — Aggressivo · AAPL: nessuno stop di uscita attivo"]
    [issue] = fake.created
    assert issue["title"] == "PROTEGGI · Aggressivo · AAPL"
    assert issue["assignee"] == "rm-1"
    assert "qty: 10 · prezzo medio: 180 USD" in issue["description"]
    # a new instance (worker restart) sends nothing again
    clock.t = NOW + timedelta(minutes=5)
    await _alarm(sm, clock, fake).tick()
    assert len(await _outbox(sm)) == 1 and len(fake.created) == 1
    async with sm() as s:
        state = await load_state(s)
        [entry] = state["open"].values()
        assert entry["issue"] == "SMT-1"
    assert await _activity_count(sm) == activity_before  # nothing reaches the agents


async def test_resolution_clears_and_reappearance_alerts_again(env):
    sm = env
    pid = await _portfolio(sm)
    await _position(sm, pid)
    clock, fake = Clock(NOW), FakeIssues()
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()
    assert len(await _outbox(sm)) == 1
    assert fake.created == []  # Paperclip not configured: Telegram only
    stop = await _order(sm, pid)
    clock.t += timedelta(minutes=5)
    await alarm.tick()
    async with sm() as s:
        assert (await load_state(s))["open"] == {}
    await _set_order(sm, stop, status=OrderStatus.canceled)
    clock.t += timedelta(minutes=5)
    await alarm.tick()
    assert len(await _outbox(sm)) == 2


async def test_day_bracket_near_the_close(env):
    """Tonight's case: a bracket with tif day, its stop and target expiring at the close."""
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    group = uuid.uuid4()
    await _order(sm, pid, tif=TimeInForce.day, expires_at=CLOSE, oco_group_id=group,
                 role="stop_loss")
    await _order(sm, pid, type=OrderType.limit, tif=TimeInForce.day, expires_at=CLOSE,
                 oco_group_id=group, role="take_profit")
    clock, fake = Clock(NOW), FakeIssues()
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()
    assert await _outbox(sm) == []  # three hours to the close: fine
    clock.t = CLOSE - timedelta(minutes=25)
    await alarm.tick()
    [text] = await _outbox(sm)
    assert text.endswith("lo stop vale solo per la giornata (tif day, scade il 16/11 22:00)")
    [issue] = fake.created
    assert "tif gtc" in issue["description"] and "take_profit" in issue["description"]


async def test_pending_bracket_entry_partial_fill_archived_and_grace(env):
    sm = env
    pid = await _portfolio(sm)
    archived = await _portfolio(sm, "Archiviato", PortfolioStatus.archived)
    await _position(sm, archived)
    # a bracket whose entry has not filled: no position, nothing to protect
    entry = await _order(sm, pid, side=OrderSide.buy, type=OrderType.limit, role="entry",
                         symbol="MSFT")
    await _order(sm, pid, symbol="MSFT", status=OrderStatus.held, parent_id=entry,
                 role="stop_loss")
    # a partially filled entry: its stop is still held
    entry2 = await _order(sm, pid, side=OrderSide.buy, type=OrderType.limit, role="entry",
                          status=OrderStatus.partially_filled, filled_qty=D(3))
    await _order(sm, pid, status=OrderStatus.held, parent_id=entry2, role="stop_loss")
    await _position(sm, pid, qty="3")
    # changed a minute ago: left to the next run
    await _position(sm, pid, symbol="NVDA", updated=NOW - timedelta(minutes=1))
    await _alarm(sm, Clock(NOW), FakeIssues()).tick()
    assert await _outbox(sm) == [
        ("⚠️ Posizione senza protezione — Aggressivo · AAPL: nessuno stop di uscita attivo "
        "(stop in attesa: l'ingresso è eseguito solo in parte)")]


async def test_paperclip_failure_is_recorded_and_retried(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    await _order(sm, pid, qty="4")  # partial cover
    clock, fake = Clock(NOW), FakeIssues()
    fake.fail = "HTTP 503"
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()
    assert await _outbox(sm) == [
        "⚠️ Posizione senza protezione — Aggressivo · AAPL: lo stop copre 4 su 10"]
    async with sm() as s:
        state = await load_state(s)
    assert state["last_error"] == "HTTP 503"
    assert [e["attempts"] for e in state["open"].values()] == [1]
    fake.fail = None
    clock.t += timedelta(minutes=5)
    await alarm.tick()
    assert len(fake.created) == 1 and len(await _outbox(sm)) == 1
    async with sm() as s:
        state = await load_state(s)
    assert state["last_error"] is None
    assert [e["issue"] for e in state["open"].values()] == ["SMT-1"]


async def test_paperclip_gives_up_after_attempts_and_missing_agent(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    clock, fake = Clock(NOW), FakeIssues()
    fake.agents_list = [{"id": "ceo", "name": "CEO"}]
    alarm = _alarm(sm, clock, fake)
    for _ in range(ISSUE_ATTEMPTS + 2):
        await alarm.tick()
        clock.t += timedelta(minutes=5)
    assert len(fake.configs) == ISSUE_ATTEMPTS
    async with sm() as s:
        state = await load_state(s)
    assert state["last_error"] == "agente «Risk Manager» non trovato"


async def test_disabled_paperclip_sends_telegram_only(env):
    sm = env
    await _paperclip(sm, enabled=False)
    pid = await _portfolio(sm)
    await _position(sm, pid, qty="-5")
    fake = FakeIssues()
    await _alarm(sm, Clock(NOW), fake).tick()
    assert len(await _outbox(sm)) == 1
    assert fake.configs == []


async def test_new_problem_of_an_issued_position_reuses_its_issue(env):
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    stop = await _order(sm, pid, qty="4", tif=TimeInForce.day, expires_at=CLOSE)
    clock, fake = Clock(NOW), FakeIssues()
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()  # short cover only
    clock.t = CLOSE - timedelta(minutes=10)
    await alarm.tick()  # and now day tif near the close
    assert len(await _outbox(sm)) == 2
    assert len(fake.created) == 1
    async with sm() as s:
        state = await load_state(s)
    assert sorted(e["issue"] for e in state["open"].values()) == ["SMT-1", "SMT-1"]
    assert stop


async def test_position_in_grace_keeps_its_open_problems(env):
    """A position changed after its alert is skipped for GRACE, not resolved: no second
    Telegram warning, no second «PROTEGGI» issue."""
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    clock, fake = Clock(NOW), FakeIssues()
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()
    assert len(await _outbox(sm)) == 1 and len(fake.created) == 1
    clock.t = NOW + timedelta(minutes=5)
    async with sm() as s:
        await s.execute(update(Position).where(Position.portfolio_id == pid)
                        .values(updated_at=clock.t - timedelta(minutes=1)))
        await s.commit()
    await alarm.tick()  # skipped: the entry stays
    async with sm() as s:
        assert len((await load_state(s))["open"]) == 1
    clock.t += timedelta(minutes=5)
    await alarm.tick()
    assert len(await _outbox(sm)) == 1 and len(fake.created) == 1


async def test_stop_being_modified_protects_and_recent_exit_order_means_grace(env):
    sm = env
    pid = await _portfolio(sm)
    await _position(sm, pid)
    stop = await _order(sm, pid)
    await _set_order(sm, stop, broker_status="replace_requested",
                     updated_at=NOW - timedelta(hours=1))
    await _position(sm, pid, symbol="MSFT")
    stop2 = await _order(sm, pid, symbol="MSFT")
    # canceled a minute ago: the agent may be replacing it right now
    await _set_order(sm, stop2, broker_status="cancel_requested",
                     updated_at=NOW - timedelta(minutes=1))
    clock = Clock(NOW)
    alarm = _alarm(sm, clock, FakeIssues())
    await alarm.tick()
    assert await _outbox(sm) == []
    clock.t += timedelta(minutes=6)
    await alarm.tick()
    assert await _outbox(sm) == [
        "⚠️ Posizione senza protezione — Aggressivo · MSFT: nessuno stop di uscita attivo"]


async def test_timeout_checks_paperclip_before_creating_again(env):
    """A timed-out POST may have created the issue: the retry looks for an open issue with
    the same title first and adopts it."""
    sm = env
    await _paperclip(sm)
    pid = await _portfolio(sm)
    await _position(sm, pid)
    clock, fake = Clock(NOW), FakeIssues()
    fake.fail = "timeout"
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()
    assert fake.lookups == []  # a first attempt does not look
    fake.fail = None
    fake.existing = [{"title": "PROTEGGI · Aggressivo · AAPL", "identifier": "SMT-9"}]
    clock.t += timedelta(minutes=5)
    await alarm.tick()
    assert fake.created == [] and fake.lookups == ["PROTEGGI · Aggressivo · AAPL"]
    async with sm() as s:
        state = await load_state(s)
    assert [e["issue"] for e in state["open"].values()] == ["SMT-9"]


async def test_config_is_read_only_when_an_issue_is_pending(env, monkeypatch):
    from smtrading.positions import protection

    calls = []
    real = protection.load_config

    async def counting(session):
        calls.append(1)
        return await real(session)

    monkeypatch.setattr(protection, "load_config", counting)
    sm = env
    await _paperclip(sm)
    clock, fake = Clock(NOW), FakeIssues()
    alarm = _alarm(sm, clock, fake)
    await alarm.tick()  # nothing to protect
    assert calls == []
    pid = await _portfolio(sm)
    await _position(sm, pid)
    await alarm.tick()
    assert calls and len(fake.created) == 1
