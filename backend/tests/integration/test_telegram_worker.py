"""Worker side of Telegram: the activity planner and the outbox sender (respx only: the
real Telegram API is never called)."""

import asyncio
import contextlib
import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.agents.models import McpAuditLog
from smtrading.common.db import make_engine
from smtrading.events import discard_post_commit, queue_event
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import AppSetting
from smtrading.seed import run_seed
from smtrading.telegram import config as tg_config
from smtrading.telegram import outbox as outbox_mod
from smtrading.telegram import planner as planner_mod
from smtrading.telegram import sender as sender_mod
from smtrading.telegram.client import API_URL, TelegramClient, TelegramError, kind_of_message
from smtrading.telegram.config import TelegramConfig, load_config, save_config
from smtrading.telegram.models import TelegramOutbox
from smtrading.telegram.outbox import enqueue, resume_paused, retry_outbox
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.sender import MAX_ATTEMPTS, SEND_MIN_INTERVAL_S, OutboxSender
from smtrading.telegram.settings import release_hold

TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
SEND_URL = f"{API_URL}/bot{TOKEN}/sendMessage"
CHAT = "-100123"
TZ = "Europe/Rome"
# future instants, so rows created now (server clock) are already due for the fake clock
DAY = datetime(2027, 3, 10, 9, 0, tzinfo=UTC)  # Wednesday 10:00 Rome
NIGHT = datetime(2027, 3, 10, 22, 0, tzinfo=UTC)  # 23:00:00 Rome


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _clear_warned():
    tg_config._warned.clear()
    yield
    tg_config._warned.clear()


@pytest.fixture
async def maker(fresh_db, redis_url, monkeypatch):
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


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


async def _configure(maker, **kw) -> None:
    cfg = TelegramConfig(bot_token=TOKEN, chat_id=CHAT, enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    async with maker() as s:
        await save_config(s, cfg)
        await s.commit()


async def _fill(maker, name="Test 250 A") -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=f"{name} {uuid.uuid4().hex[:4]}",
                                   initial_cash=Decimal(0))
        queue_event(s, "order.filled", {
            "portfolio_id": p.id, "order_id": uuid.uuid4(), "fill_id": uuid.uuid4(),
            "side": "buy", "qty": Decimal(2), "symbol": "AAPL", "price": Decimal("200.5"),
            "currency": "USD"})
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _kill_switch(maker) -> None:
    async with maker() as s:
        queue_event(s, "kill_switch", {"on": True, "canceled": 3})
        discard_post_commit(s)
        await s.commit()


async def _queue_texts(maker, *texts: str) -> list[int]:
    async with maker() as s:
        ids = [(await enqueue(s, source="system", category="event", level="info",
                              text=t)).id for t in texts]
        await s.commit()
    return ids


async def _outbox(maker) -> list[TelegramOutbox]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _activity(maker) -> list[Activity]:
    async with maker() as s:
        return list(await s.scalars(
            select(Activity).where(Activity.telegram.is_not(None)).order_by(Activity.id)))


def _ok(message_id=1):
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def _err(status, description, **parameters):
    body = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


def _sent(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


def _sender(maker, http, clock) -> tuple[OutboxSender, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    return OutboxSender(maker, http, clock=clock, tz=TZ, sleep=sleep), sleeps


async def _drain(sender: OutboxSender, n: int = 20) -> None:
    for _ in range(n):
        await sender.tick()


# -- planner --------------------------------------------------------------------------


@respx.mock
async def test_fill_reaches_group(maker, http):
    await _configure(maker)
    pid = await _fill(maker)
    clock = Clock(DAY)
    assert await TelegramPlanner(maker, clock=clock, tz=TZ).tick() == 1
    [a] = await _activity(maker)
    assert a.telegram == "queued" and a.outbox_id is not None
    route = respx.post(SEND_URL).mock(return_value=_ok(77))
    sender, _ = _sender(maker, http, clock)
    assert await sender.tick() == 1
    [body] = _sent(route)
    assert body["chat_id"] == CHAT
    assert body["text"].startswith("🟢 Ordine eseguito — Test 250 A")
    assert "AAPL" in body["text"]
    assert body["disable_notification"] is False
    assert "parse_mode" not in body
    [row] = await _outbox(maker)
    assert (row.status, row.telegram_message_id, row.sent_at) == ("sent", 77, DAY)
    assert row.id == a.outbox_id
    assert pid is not None


@respx.mock
@pytest.mark.parametrize("cfg", [{"enabled": False}, {"bot_token": None}, {"chat_id": None},
                                 None])
async def test_disabled_config_skips(maker, http, cfg):
    if cfg is not None:
        await _configure(maker, **cfg)
    await _fill(maker)
    await _kill_switch(maker)
    route = respx.post(SEND_URL).mock(return_value=_ok())
    clock = Clock(DAY)
    assert await TelegramPlanner(maker, clock=clock, tz=TZ).tick() == 2
    assert [a.telegram for a in await _activity(maker)] == ["skipped", "skipped"]
    assert await _outbox(maker) == []
    sender, _ = _sender(maker, http, clock)
    assert await sender.tick() == 0
    assert not route.called


@respx.mock
async def test_sender_never_sends_while_config_not_ready(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    await _configure(maker, enabled=False)
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 0
    async with maker() as s:  # unreadable token (other master key): not ready either
        await s.execute(update(AppSetting).where(AppSetting.key == "telegram").values(
            value={"token_enc": "AAAA", "chat_id": CHAT, "enabled": True}))
        await s.commit()
    assert await sender.tick() == 0
    assert not route.called
    assert [r.status for r in await _outbox(maker)] == ["pending"]


@respx.mock
async def test_quiet_noncritical_goes_to_digest_critical_sent_silent(maker, http):
    await _configure(maker)
    await _fill(maker)
    await _kill_switch(maker)
    clock = Clock(NIGHT)  # 23:00:00 Europe/Rome
    assert await TelegramPlanner(maker, clock=clock, tz=TZ).tick() == 2
    fill, ks = await _activity(maker)
    assert (fill.telegram, fill.outbox_id) == ("digest", None)
    assert ks.telegram == "queued"
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sender, _ = _sender(maker, http, clock)
    await _drain(sender, 3)
    [body] = _sent(route)
    assert body["text"].startswith("🔴 Kill switch")
    assert body["disable_notification"] is True


async def test_planner_just_before_night_sends_now(maker):
    await _configure(maker)
    await _fill(maker)
    clock = Clock(NIGHT - timedelta(seconds=1))  # 22:59:59
    await TelegramPlanner(maker, clock=clock, tz=TZ).tick()
    [a] = await _activity(maker)
    assert a.telegram == "queued"


async def test_planner_paused_config_queues_paused(maker):
    await _configure(maker, paused=True)
    await _fill(maker)
    await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick()
    [a] = await _activity(maker)
    assert a.telegram == "queued"
    assert [r.status for r in await _outbox(maker)] == ["paused"]


# -- sender ---------------------------------------------------------------------------


@respx.mock
async def test_paused_then_resumed(maker, http):
    await _configure(maker, paused=True)
    await _queue_texts(maker, "uno", "due", "tre")
    route = respx.post(SEND_URL).mock(return_value=_ok())
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    await _drain(sender, 3)
    assert not route.called
    assert {r.status for r in await _outbox(maker)} == {"paused"}
    await _configure(maker, paused=False)
    async with maker() as s:
        assert await resume_paused(s) == 3
        await s.commit()
    await _drain(sender, 5)
    assert [b["text"] for b in _sent(route)] == ["uno", "due", "tre"]
    assert {r.status for r in await _outbox(maker)} == {"sent"}


@respx.mock
async def test_pending_rows_wait_while_paused(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    await _configure(maker, paused=True)
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 0
    assert not route.called


@respx.mock
async def test_429_respects_retry_after_without_attempt(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    route = respx.post(SEND_URL).mock(side_effect=[
        _err(429, "Too Many Requests: retry after 30", retry_after=30), _ok(1), _ok(2)])
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    assert await sender.tick() == 1
    first, second = await _outbox(maker)
    assert (first.status, first.attempts) == ("pending", 0)
    assert first.next_attempt_at == DAY + timedelta(seconds=30)
    assert "attendere 30 s" in first.last_error
    assert second.next_attempt_at == DAY + timedelta(seconds=30)  # the hold is persisted
    clock.advance(29)
    assert await sender.tick() == 0  # global pause: not even the second message
    assert route.call_count == 1
    clock.advance(1)
    await _drain(sender, 3)
    assert [b["text"] for b in _sent(route)] == ["uno", "uno", "due"]
    rows = await _outbox(maker)
    assert [(r.status, r.attempts) for r in rows] == [("sent", 0), ("sent", 0)]


@respx.mock
async def test_429_hold_survives_a_restart(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    route = respx.post(SEND_URL).mock(side_effect=[
        _err(429, "Too Many Requests", retry_after=60)])
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    await sender.tick()
    restarted, _ = _sender(maker, http, clock)
    await _queue_texts(maker, "tre")  # queued after the restart: not pushed by the hold
    route.side_effect = [_ok(1), _ok(2), _ok(3)]
    clock.advance(10)
    assert await restarted.tick() == 0  # "due"/"tre" must not overtake "uno"
    clock.advance(50)
    await _drain(restarted, 4)
    assert [b["text"] for b in _sent(route)] == ["uno", "uno", "due", "tre"]


@respx.mock
async def test_outage_then_recovery_keeps_order(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due", "tre")
    route = respx.post(SEND_URL).mock(side_effect=[
        httpx.ConnectError("down"), httpx.ConnectError("down"), _ok(1), _ok(2), _ok(3)])
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    for _ in range(30):  # one simulated hour of ticks a few seconds apart
        await sender.tick()
        clock.advance(2)
    assert [b["text"] for b in _sent(route)] == ["uno", "uno", "uno", "due", "tre"]
    rows = await _outbox(maker)
    assert [r.status for r in rows] == ["sent"] * 3
    assert [r.attempts for r in rows] == [2, 0, 0]  # only the head spent attempts


@respx.mock
async def test_ten_failures_mark_failed(maker, http):
    await _configure(maker)
    [oid] = await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(
        return_value=_err(400, "Bad Request: can't parse entities"))
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    delays = []
    for _ in range(MAX_ATTEMPTS):
        start = clock()
        assert await sender.tick() == 1
        [row] = await _outbox(maker)
        if row.status == "failed":
            break
        delays.append((row.next_attempt_at - start).total_seconds())
        clock.advance(1)
        assert await sender.tick() == 0  # still in backoff
        clock.t = row.next_attempt_at
    assert delays == [5, 10, 20, 40, 80, 160, 320, 640, 900]
    [row] = await _outbox(maker)
    assert (row.status, row.attempts) == ("failed", MAX_ATTEMPTS)
    assert route.call_count == MAX_ATTEMPTS
    assert row.last_error.startswith("Errore Telegram (codice 400)")
    async with maker() as s:  # a failed message can be retried
        await retry_outbox(s, oid)
        await s.commit()
    route.side_effect = None
    route.return_value = _ok(5)
    clock.advance(SEND_MIN_INTERVAL_S)
    assert await sender.tick() == 1
    [row] = await _outbox(maker)
    assert row.status == "sent"


@respx.mock
async def test_message_error_backs_off_only_that_message(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    route = respx.post(SEND_URL).mock(side_effect=[
        _err(400, "Bad Request: can't parse entities"), _ok(2)])
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    await sender.tick()
    await sender.tick()
    assert [b["text"] for b in _sent(route)] == ["uno", "due"]
    first, second = await _outbox(maker)
    assert (first.status, first.attempts) == ("pending", 1)
    assert second.status == "sent"


@respx.mock
async def test_supergroup_migration_updates_config_and_resends(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(side_effect=[
        _err(400, "Bad Request: group chat was upgraded to a supergroup chat",
             migrate_to_chat_id=-1009876543210),
        _ok(9)])
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 1
    assert [b["chat_id"] for b in _sent(route)] == [CHAT, "-1009876543210"]
    [row] = await _outbox(maker)
    assert (row.status, row.attempts, row.telegram_message_id) == ("sent", 0, 9)
    async with maker() as s:
        cfg = await load_config(s)
        audit = list(await s.scalars(select(McpAuditLog).where(
            McpAuditLog.method == "admin:telegram.chat_migrated")))
    assert cfg.chat_id == "-1009876543210"
    assert (cfg.bot_token, cfg.enabled) == (TOKEN, True)
    [a] = audit
    assert a.params == {"old": CHAT, "new": "-1009876543210", "by": "system"}
    assert a.agent_id is None


@respx.mock
async def test_last_error_has_no_token(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    respx.post(SEND_URL).mock(side_effect=[
        _err(400, f"Bad Request: bot{TOKEN} is wrong"), httpx.ConnectError(f"{SEND_URL}")])
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    await sender.tick()
    await sender.tick()
    rows = await _outbox(maker)
    assert all(r.last_error for r in rows)
    for r in rows:
        assert TOKEN not in r.last_error
        assert "AAHsecret" not in r.last_error


@respx.mock
async def test_invalid_stored_token_is_an_error_not_a_crash(maker, http):
    await _configure(maker, bot_token="not a token\n")
    await _queue_texts(maker, "uno")
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 1
    [row] = await _outbox(maker)
    assert (row.status, row.attempts, row.last_error) == (
        "pending", 1, "Token del bot non valido")


@respx.mock
async def test_min_interval_between_sends(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due", "tre")
    times: list[datetime] = []
    clock = Clock(DAY)

    def record(request):
        times.append(clock())
        return _ok()

    respx.post(SEND_URL).mock(side_effect=record)
    sender, sleeps = _sender(maker, http, clock)
    await _drain(sender, 3)
    assert len(times) == 3
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
    assert all(g >= SEND_MIN_INTERVAL_S for g in gaps)
    assert sleeps == [SEND_MIN_INTERVAL_S, SEND_MIN_INTERVAL_S]


@respx.mock
async def test_quiet_evaluated_at_send_time(maker, http):
    """An agent message queued by day but sent at night goes out silent."""
    await _configure(maker)
    await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sender, _ = _sender(maker, http, Clock(NIGHT + timedelta(hours=7, seconds=59 * 60 + 59)))
    await sender.tick()  # 06:59:59 Rome: still quiet
    assert _sent(route)[0]["disable_notification"] is True


@respx.mock
async def test_sent_without_message_id_is_not_retried(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    await sender.tick()
    clock.advance(60)
    await sender.tick()
    assert route.call_count == 1
    [row] = await _outbox(maker)
    assert (row.status, row.telegram_message_id) == ("sent", None)


@respx.mock
async def test_three_hour_outage_then_recovery_none_failed(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due", "tre")
    clock = Clock(DAY)
    end = DAY + timedelta(hours=3)

    def answer(request):
        if clock() < end:
            return httpx.Response(502, text="bad gateway")
        return _ok()

    route = respx.post(SEND_URL).mock(side_effect=answer)
    sender, _ = _sender(maker, http, clock)
    while clock() < end + timedelta(minutes=20):
        await sender.tick()
        clock.advance(30)
    texts = [b["text"] for b in _sent(route)]
    assert texts[-3:] == ["uno", "due", "tre"]
    assert set(texts[:-3]) == {"uno"}  # only the head was retried during the outage
    rows = await _outbox(maker)
    assert [r.status for r in rows] == ["sent"] * 3
    assert rows[0].attempts > MAX_ATTEMPTS  # counted, never failed
    assert [r.attempts for r in rows[1:]] == [0, 0]


@respx.mock
@pytest.mark.parametrize("response", [
    _err(403, "Forbidden: bot was kicked from the group chat"),
    _err(400, "Bad Request: not enough rights to send text messages to the chat"),
    _err(400, "Bad Request: chat not found"),
])
async def test_config_error_holds_everything_then_fails_head(maker, http, response):
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    route = respx.post(SEND_URL).mock(return_value=response)
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    for _ in range(MAX_ATTEMPTS):
        assert await sender.tick() == 1
        first, second = await _outbox(maker)
        assert second.attempts == 0  # held, not tried
        clock.t = max(first.next_attempt_at, second.next_attempt_at)
    first, second = await _outbox(maker)
    assert (first.status, first.attempts) == ("failed", MAX_ATTEMPTS)
    assert route.call_count == MAX_ATTEMPTS
    assert {b["text"] for b in _sent(route)} == {"uno"}
    clock.advance(-1)
    assert await sender.tick() == 0  # the hold stays after the head failed
    clock.advance(1)
    route.return_value = _ok()
    assert await sender.tick() == 1
    assert _sent(route)[-1]["text"] == "due"


@respx.mock
async def test_migration_to_same_id_writes_no_audit(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(side_effect=[
        _err(400, "Bad Request: group chat was upgraded", migrate_to_chat_id=int(CHAT)),
        _ok(3)])
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 1
    assert route.call_count == 2
    async with maker() as s:
        audit = list(await s.scalars(select(McpAuditLog).where(
            McpAuditLog.method == "admin:telegram.chat_migrated")))
    assert audit == []


@respx.mock
async def test_unreadable_config_keeps_pending_until_token_reentered(maker, http):
    await _configure(maker)
    await _fill(maker)
    async with maker() as s:
        await s.execute(update(AppSetting).where(AppSetting.key == "telegram").values(
            value={"token_enc": "AAAA", "chat_id": CHAT, "enabled": True}))
        await s.commit()
    clock = Clock(DAY)
    planner = TelegramPlanner(maker, clock=clock, tz=TZ)
    assert await planner.tick() == 0
    assert [a.telegram for a in await _activity(maker)] == ["pending"]
    await _configure(maker)  # token entered again
    assert await planner.tick() == 1
    assert [a.telegram for a in await _activity(maker)] == ["queued"]
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sender, _ = _sender(maker, http, clock)
    assert await sender.tick() == 1
    assert _sent(route)[0]["text"].startswith("🟢 Ordine eseguito")


@pytest.mark.parametrize("stored", [
    {"token_enc": "AAAA", "chat_id": CHAT, "enabled": False},  # disabled
    {"token_enc": "AAAA", "chat_id": None, "enabled": True},  # no group
])
async def test_unreadable_and_disabled_skips(maker, stored):
    """Disabled (or no group) wins over unreadable: the rows are skipped, not kept for weeks
    and sent when the token is entered again."""
    await _fill(maker)
    async with maker() as s:
        s.add(AppSetting(key="telegram", value=stored))
        await s.commit()
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 1
    assert [a.telegram for a in await _activity(maker)] == ["skipped"]


@respx.mock
async def test_migration_keeps_concurrent_pause(maker, http, monkeypatch):
    """The supergroup migration locks the settings row before reading it: a pause written
    meanwhile (by an admin) is applied after it, never overwritten."""
    await _configure(maker)
    await _queue_texts(maker, "uno")
    respx.post(SEND_URL).mock(side_effect=[
        _err(400, "Bad Request: group chat was upgraded", migrate_to_chat_id=-100999),
        _ok(3)])
    real = sender_mod.load_config
    calls, pauses = 0, []

    async def pause() -> None:
        async with maker() as other:
            await other.execute(text(
                "update app_settings set value = jsonb_set(value, '{paused}', 'true') "
                "where key = 'telegram'"))
            await other.commit()

    async def load_then_pause(session):
        nonlocal calls
        calls += 1
        cfg = await real(session)
        if calls == 2:  # inside _migrate: an admin pauses Telegram right now
            pauses.append(asyncio.create_task(pause()))
            await asyncio.sleep(0.3)  # committed by now unless the row is locked
        return cfg

    monkeypatch.setattr(sender_mod, "load_config", load_then_pause)
    sender, _ = _sender(maker, http, Clock(DAY))
    assert await sender.tick() == 1
    await asyncio.gather(*pauses)
    async with maker() as s:
        cfg = await real(s)
    assert (cfg.chat_id, cfg.paused) == ("-100999", True)


@respx.mock
async def test_config_fix_releases_hold(maker, http):
    """A chat-wide config error holds the queue up to 900 s; saving a new token or group
    (settings version changes) releases the hold at once."""
    await _configure(maker)
    await _queue_texts(maker, "uno", "due")
    route = respx.post(SEND_URL).mock(return_value=_err(403, "Forbidden: bot was kicked"))
    clock = Clock(DAY)
    sender, _ = _sender(maker, http, clock)
    for _ in range(6):  # backoff now well above a minute
        assert await sender.tick() == 1
        first, _second = await _outbox(maker)
        clock.t = first.next_attempt_at
    assert await sender.tick() == 1
    clock.advance(1)
    assert await sender.tick() == 0  # held
    route.return_value = _ok()
    async with maker() as s:  # the admin fixes the group (same path as PUT /config)
        await release_hold(s)
        await s.commit()
    assert await sender.tick() == 1
    assert _sent(route)[-1]["text"] == "uno"
    clock.advance(SEND_MIN_INTERVAL_S)
    assert await sender.tick() == 1
    assert [r.status for r in await _outbox(maker)] == ["sent", "sent"]


async def test_stale_noncritical_skipped_critical_sent(maker):
    await _configure(maker)
    await _fill(maker)
    await _kill_switch(maker)
    async with maker() as s:
        await s.execute(text("update activity set ts = now() - interval '25 hours'"))
        await s.commit()
    await _fill(maker)  # recent
    await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick()
    assert [(a.level, a.telegram) for a in await _activity(maker)] == [
        ("executed", "skipped"), ("critical", "queued"), ("executed", "queued")]


async def test_bad_row_does_not_poison_the_batch(maker, monkeypatch, caplog):
    await _configure(maker)
    await _fill(maker)
    await _kill_switch(maker)
    await _fill(maker)
    real = planner_mod.render_activity

    def render(a, names, tz):
        if a.level == "critical":
            raise RuntimeError("boom")
        return real(a, names, tz)

    monkeypatch.setattr(planner_mod, "render_activity", render)
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 3
    assert [a.telegram for a in await _activity(maker)] == ["queued", "failed", "queued"]
    assert len(await _outbox(maker)) == 2
    assert any("cannot be planned" in r.getMessage() for r in caplog.records)


async def test_planner_loads_config_once_per_batch(maker, monkeypatch):
    await _configure(maker)
    for _ in range(3):
        await _fill(maker)
    calls = []
    real = outbox_mod.load_config

    async def counting(session):
        calls.append(1)
        return await real(session)

    monkeypatch.setattr(outbox_mod, "load_config", counting)
    assert await TelegramPlanner(maker, clock=Clock(DAY), tz=TZ).tick() == 3
    assert calls == []


@respx.mock
async def test_single_sender_holds_advisory_lock(maker, http):
    await _configure(maker)
    await _queue_texts(maker, "uno")
    route = respx.post(SEND_URL).mock(return_value=_ok())
    engine = maker.kw["bind"]
    async with engine.connect() as other:
        assert await other.scalar(text("select pg_try_advisory_lock(:k)"),
                                  {"k": sender_mod.ADVISORY_LOCK_KEY})
        await other.commit()
        task = asyncio.create_task(OutboxSender(maker, http, interval_s=0.05).run())
        try:
            await asyncio.sleep(0.5)
            assert not route.called  # another sender holds the lock
            await other.execute(text("select pg_advisory_unlock(:k)"),
                                {"k": sender_mod.ADVISORY_LOCK_KEY})
            await other.commit()
            for _ in range(100):
                if route.called:
                    break
                await asyncio.sleep(0.05)
            assert route.call_count == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@respx.mock
@pytest.mark.parametrize(("response", "kind"), [
    (httpx.ConnectError("down"), "transient"),
    (httpx.Response(502, text="bad gateway"), "transient"),
    (_err(429, "Too Many Requests", retry_after=3), "transient"),
    (_err(401, "Unauthorized"), "chat"),
    (_err(404, "Not Found"), "chat"),
    (_err(403, "Forbidden: bot was kicked"), "chat"),
    (_err(400, "Bad Request: chat not found"), "chat"),
    (_err(400, "Bad Request: not enough rights to send text messages to the chat"), "chat"),
    (_err(400, "Bad Request: have no rights to send a message"), "chat"),
    (_err(400, "Bad Request: can't parse entities"), "message"),
])
async def test_error_kinds(http, response, kind):
    if isinstance(response, Exception):
        respx.post(SEND_URL).mock(side_effect=response)
    else:
        respx.post(SEND_URL).mock(return_value=response)
    with pytest.raises(TelegramError) as info:
        await TelegramClient(TOKEN, http).send_message(CHAT, "x")
    assert info.value.kind == kind
    assert info.value.chat_wide is (kind != "message")
    assert kind_of_message(info.value.message) == kind
