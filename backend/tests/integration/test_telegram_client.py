import json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text, update

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.risk.models import AppSetting
from smtrading.telegram.client import API_URL, TelegramClient, TelegramError
from smtrading.telegram.config import (
    TelegramConfig,
    config_dto,
    is_ready,
    load_config,
    save_config,
)
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.outbox import (
    agent_sent_last_minute,
    enqueue,
    resume_paused,
    retry_outbox,
)

TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-1234567"
BASE = f"{API_URL}/bot{TOKEN}"


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


def _ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def _err(status, description, **parameters):
    body = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(status, json=body)


@respx.mock
async def test_send_message_payload_plain_text():
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=_ok({"message_id": 42}))
    async with httpx.AsyncClient() as http:
        mid = await TelegramClient(TOKEN, http).send_message(-100, "<b>*_[x](y)@all")
    assert mid == 42
    body = json.loads(route.calls.last.request.content)
    assert body == {"chat_id": -100, "text": "<b>*_[x](y)@all", "disable_notification": False,
                    "link_preview_options": {"is_disabled": True}}
    assert "parse_mode" not in body


@respx.mock
async def test_send_message_with_entities_and_silent():
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=_ok({"message_id": 7}))
    entities = [{"type": "pre", "offset": 3, "length": 4}]
    async with httpx.AsyncClient() as http:
        await TelegramClient(TOKEN, http).send_message("-100", "ab\nabcd", entities=entities,
                                                       disable_notification=True)
    body = json.loads(route.calls.last.request.content)
    assert body["entities"] == entities
    assert body["disable_notification"] is True
    assert "parse_mode" not in body


@pytest.mark.parametrize(("response", "message", "status", "retry", "migrate"), [
    (_err(401, "Unauthorized"), "Token del bot non valido", 401, None, None),
    (_err(400, "Bad Request: chat not found"),
     "Gruppo non trovato: controlla l'ID e che il bot sia nel gruppo", 400, None, None),
    (_err(403, "Forbidden: bot was kicked from the group chat"),
     "Il bot non è membro del gruppo o è stato rimosso", 403, None, None),
    (_err(429, "Too Many Requests: retry after 7", retry_after=7),
     "Telegram chiede di attendere 7 s", 429, 7, None),
    (_err(400, "Bad Request: group chat was upgraded to a supergroup chat",
          migrate_to_chat_id=-100123), None, 400, None, -100123),
    (_err(400, f"Bad Request: weird {TOKEN}"), None, 400, None, None),
    (httpx.Response(502, text="<html>bad gateway</html>"), "Errore Telegram (codice 502)", 502,
     None, None),
])
@respx.mock
async def test_errors_mapped_italian_without_token(response, message, status, retry, migrate):
    respx.post(f"{BASE}/sendMessage").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as info:
            await TelegramClient(TOKEN, http).send_message(-1, "x")
    e = info.value
    if message:
        assert e.message.startswith(message)
    assert (e.status, e.retry_after, e.migrate_to_chat_id) == (status, retry, migrate)
    _assert_no_token(e)


@respx.mock
async def test_connect_error_mapped_without_token():
    respx.post(f"{BASE}/getMe").mock(side_effect=httpx.ConnectError(f"boom {BASE}/getMe"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as info:
            await TelegramClient(TOKEN, http).get_me()
    assert info.value.message == "Telegram non raggiungibile"
    assert info.value.status is None
    _assert_no_token(info.value)


@respx.mock
async def test_webhook_conflict_on_get_updates():
    respx.post(f"{BASE}/getUpdates").mock(
        return_value=_err(409, "Conflict: can't use getUpdates method while webhook is active"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as info:
            await TelegramClient(TOKEN, http).get_updates()
    assert info.value.message == "Il bot ha un webhook attivo: rimuovilo con deleteWebhook"


def _assert_no_token(e: TelegramError):
    secret = TOKEN.split(":", 1)[1]
    for s in (str(e), repr(e), e.message, repr(e.__cause__), repr(e.__context__)):
        assert secret not in s
    assert e.__cause__ is None and e.__context__ is None
    assert e.__suppress_context__ is True


@respx.mock
async def test_get_updates_has_no_offset():
    route = respx.post(f"{BASE}/getUpdates").mock(return_value=_ok([{"update_id": 1}]))
    async with httpx.AsyncClient() as http:
        updates = await TelegramClient(TOKEN, http).get_updates(limit=500)
    assert updates == [{"update_id": 1}]
    body = json.loads(route.calls.last.request.content)
    assert "offset" not in body
    assert body == {"limit": 100}


@respx.mock
async def test_get_me_and_get_chat():
    respx.post(f"{BASE}/getMe").mock(return_value=_ok(
        {"id": 123456789, "is_bot": True, "first_name": "SMT", "username": "smt_bot"}))
    chat = respx.post(f"{BASE}/getChat").mock(return_value=_ok(
        {"id": -100123, "type": "supergroup", "title": "Trading"}))
    async with httpx.AsyncClient() as http:
        client = TelegramClient(TOKEN, http)
        assert await client.get_me() == {"id": 123456789, "username": "smt_bot",
                                         "first_name": "SMT"}
        assert await client.get_chat(-100123) == {"id": -100123, "type": "supergroup",
                                                  "title": "Trading"}
    assert json.loads(chat.calls.last.request.content) == {"chat_id": -100123}


async def test_config_roundtrip_encrypted(db_session):
    assert await load_config(db_session) == TelegramConfig()
    cfg = TelegramConfig(bot_token=TOKEN, chat_id="-100123", enabled=True, paused=False,
                         bot_username="smt_bot", chat_title="Trading")
    await save_config(db_session, cfg)
    raw = await db_session.scalar(select(AppSetting.value).where(AppSetting.key == "telegram"))
    dumped = json.dumps(raw)
    assert TOKEN not in dumped and TOKEN.split(":")[1] not in dumped
    assert set(raw) == {"token_enc", "chat_id", "enabled", "paused", "bot_username", "chat_title"}
    loaded = await load_config(db_session)
    assert loaded == cfg
    assert is_ready(loaded)
    assert TOKEN not in repr(loaded)
    dto = config_dto(loaded)
    assert dto == {"configured": True, "enabled": True, "paused": False,
                   "bot_token_masked": "••••", "bot_username": "smt_bot",
                   "chat_id": "-100123", "chat_title": "Trading"}
    assert TOKEN[-4:] not in json.dumps(dto)
    await save_config(db_session, TelegramConfig(bot_token=TOKEN, chat_id="-1", enabled=False))
    assert not is_ready(await load_config(db_session))
    assert config_dto(TelegramConfig())["bot_token_masked"] is None


async def test_enqueue_paused_when_config_paused(db_session):
    row = await enqueue(db_session, source="system", category="event", level="info", text="a")
    assert row.status == OutboxStatus.pending
    await save_config(db_session, TelegramConfig(bot_token=TOKEN, chat_id="-1", enabled=True,
                                                 paused=True))
    paused = await enqueue(db_session, source="system", category="event", level="info",
                           text="b", entities=[{"type": "pre", "offset": 0, "length": 1}])
    assert paused.status == OutboxStatus.paused
    assert paused.entities == [{"type": "pre", "offset": 0, "length": 1}]
    with pytest.raises(ValueError):
        await enqueue(db_session, source="system", category="event", level="info",
                      text="x" * 4097)
    await enqueue(db_session, source="system", category="event", level="info", text="x" * 4096)
    assert await resume_paused(db_session) == 2
    await db_session.refresh(paused)
    assert paused.status == OutboxStatus.pending


async def test_retry_only_failed(db_session):
    row = await enqueue(db_session, source="system", category="event", level="info", text="a")
    with pytest.raises(SMTError) as info:
        await retry_outbox(db_session, row.id)
    assert info.value.code == ErrorCode.INVALID_ORDER
    row.status, row.attempts, row.last_error = OutboxStatus.failed, 10, "Telegram non raggiungibile"
    await db_session.flush()
    again = await retry_outbox(db_session, row.id)
    assert (again.status, again.attempts, again.last_error) == (OutboxStatus.pending, 0, None)
    with pytest.raises(SMTError) as info:
        await retry_outbox(db_session, 10**12)
    assert info.value.code == ErrorCode.NOT_FOUND


async def test_agent_rate_counter_window(db_session):
    me, other = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        await enqueue(db_session, source=f"agent:{me}", category="agent", level="info", text="m")
    await enqueue(db_session, source=f"agent:{other}", category="agent", level="info", text="o")
    old = await enqueue(db_session, source=f"agent:{me}", category="agent", level="info",
                        text="old")
    await db_session.execute(update(TelegramOutbox).where(TelegramOutbox.id == old.id)
                             .values(created_at=text("now() - interval '61 seconds'")))
    assert await agent_sent_last_minute(db_session, me) == 3
    assert await agent_sent_last_minute(db_session, other) == 1
    assert await agent_sent_last_minute(db_session, uuid.uuid4()) == 0
