"""Admin API «Notifiche» (Telegram config, detect, test, pause, outbox, decisions), the
notify permission and the CLI `telegram set` / `agent notify`. Telegram is mocked (respx)."""

import asyncio
import io
import json
import logging
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.telegram.client import API_URL

PASSWORD = "correct-horse-battery"
TOKEN = "123456789:AAHsecretSECRETsecret_SECRET-9876"
TOKEN2 = "987654321:BBHsecretSECRETsecret_SECRET-5555"
BASE = f"{API_URL}/bot{TOKEN}"
BASE2 = f"{API_URL}/bot{TOKEN2}"
CHAT = "-1001234567890"


@pytest.fixture(autouse=True)
def _clear_warned():
    from smtrading.telegram import config

    config._warned.clear()
    yield
    config._warned.clear()


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.email, ns.redis_url = client, sm, email, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


def _ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def _err(status, description):
    return httpx.Response(status, json={"ok": False, "error_code": status,
                                        "description": description})


def _mock_ok(router, base=BASE, username="smt_bot", title="Trading"):
    me = router.post(f"{base}/getMe").mock(
        return_value=_ok({"id": 1, "username": username, "first_name": "SMT"}))
    chat = router.post(f"{base}/getChat").mock(
        return_value=_ok({"id": int(CHAT), "type": "supergroup", "title": title}))
    return me, chat


async def _configure(admin, router, **extra):
    _mock_ok(router)
    r = await admin.client.put("/api/telegram/config",
                               json={"bot_token": TOKEN, "chat_id": CHAT, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def _audits(admin, prefix="admin:"):
    from smtrading.agents.models import McpAuditLog

    async with admin.sm() as s:
        rows = (await s.scalars(select(McpAuditLog).where(McpAuditLog.method.like(f"{prefix}%"))
                                .order_by(McpAuditLog.id))).all()
    # the request_id (link to the activity log, see test_user_activity) is left out
    return [(r.method, {k: v for k, v in r.params.items() if k != "request_id"}) for r in rows]


def _no_token(text: str) -> None:
    for t in (TOKEN, TOKEN2):
        assert t not in text
        assert t.split(":")[1] not in text
        assert t[-4:] not in text


async def test_get_config_masked(admin):
    r = await admin.client.get("/api/telegram")
    assert r.status_code == 200
    assert r.json() == {
        "configured": False, "enabled": False, "paused": False, "bot_token_masked": None,
        "bot_username": None, "chat_id": None, "chat_title": None, "error": None,
        "outbox": {"pending": 0, "paused": 0, "failed": 0},
    }
    with respx.mock(assert_all_called=False) as router:
        body = await _configure(admin, router, enabled=True)
    assert body["configured"] and body["enabled"] and body["bot_token_masked"] == "••••"
    assert body["bot_username"] == "smt_bot" and body["chat_title"] == "Trading"
    assert body["chat_id"] == CHAT
    got = await admin.client.get("/api/telegram")
    _no_token(got.text)
    _no_token(json.dumps(await _audits(admin)))


async def test_put_token_verifies_getme_and_getchat(admin):
    with respx.mock(assert_all_called=False) as router:
        me, chat = _mock_ok(router)
        r = await admin.client.put("/api/telegram/config",
                                   json={"bot_token": TOKEN, "chat_id": CHAT})
        assert r.status_code == 200, r.text
        assert me.called and chat.called
        assert json.loads(chat.calls.last.request.content) == {"chat_id": CHAT}
    # nothing changed: no new verification
    with respx.mock(assert_all_called=False) as router:
        me, _ = _mock_ok(router)
        r = await admin.client.put("/api/telegram/config", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert not me.called


async def test_put_bad_token_400_italian_no_token(admin):
    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE2}/getMe").mock(return_value=_err(401, f"Unauthorized {TOKEN2}"))
        r = await admin.client.put("/api/telegram/config", json={"bot_token": TOKEN2})
    assert r.status_code == 400
    assert r.json()["message"] == "Token del bot non valido"
    assert r.json()["details"] == {"field": "bot_token"}
    _no_token(r.text)
    # configuration unchanged: still the first token
    from smtrading.telegram.config import load_config

    async with admin.sm() as s:
        assert (await load_config(s)).bot_token == TOKEN
    # malformed token rejected before any request
    r = await admin.client.put("/api/telegram/config", json={"bot_token": "nope/../x"})
    assert r.status_code == 400 and r.json()["details"]["field"] == "bot_token"


async def test_put_bad_chat_field_chat_id(admin):
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/getMe").mock(return_value=_ok({"id": 1, "username": "b"}))
        router.post(f"{BASE}/getChat").mock(return_value=_err(400, "Bad Request: chat not found"))
        r = await admin.client.put("/api/telegram/config",
                                   json={"bot_token": TOKEN, "chat_id": "-42"})
    assert r.status_code == 400 and r.json()["details"] == {"field": "chat_id"}
    assert r.json()["message"].startswith("Gruppo non trovato")
    r = await admin.client.put("/api/telegram/config", json={"chat_id": "abc"})
    assert r.status_code == 400 and r.json()["details"] == {"field": "chat_id"}
    r = await admin.client.put("/api/telegram/config", json={"chat_id": "-42"})
    assert r.status_code == 400 and r.json()["details"] == {"field": "bot_token"}  # no token yet
    r = await admin.client.put("/api/telegram/config", json={"enabled": True})
    assert r.status_code == 400 and r.json()["details"] == {"field": "enabled"}
    assert (await admin.client.get("/api/telegram")).json()["configured"] is False


async def test_put_chat_only_keeps_token(admin):
    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/getMe").mock(return_value=_ok({"id": 1, "username": "smt_bot"}))
        router.post(f"{BASE}/getChat").mock(
            return_value=_ok({"id": -1009, "type": "group", "title": "Altro"}))
        r = await admin.client.put("/api/telegram/config", json={"chat_id": -1009})
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == "-1009" and r.json()["chat_title"] == "Altro"
    from smtrading.telegram.config import load_config

    async with admin.sm() as s:
        assert (await load_config(s)).bot_token == TOKEN


async def test_token_with_whitespace_is_stripped(admin):
    with respx.mock(assert_all_called=False) as router:
        me, _ = _mock_ok(router)
        r = await admin.client.put("/api/telegram/config",
                                   json={"bot_token": f"  {TOKEN[:9]} {TOKEN[9:]}\n"})
        assert r.status_code == 200, r.text
        assert me.called  # the URL carried the cleaned token
    from smtrading.telegram.config import load_config

    async with admin.sm() as s:
        assert (await load_config(s)).bot_token == TOKEN


async def test_unreadable_config_requires_token(admin):
    from smtrading.risk.models import AppSetting

    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    async with admin.sm() as s:
        row = await s.get(AppSetting, "telegram")
        row.value = {**row.value, "token_enc": "bm90LWEtdmFsaWQtYmxvYg=="}
        await s.commit()
    got = (await admin.client.get("/api/telegram")).json()
    assert got["error"] and got["configured"] is False
    r = await admin.client.put("/api/telegram/config", json={"enabled": True})
    assert r.status_code == 400 and r.json()["details"] == {"field": "bot_token"}
    assert r.json()["message"] == got["error"]
    # pausing keeps the stored (unreadable) blob as it is
    assert (await admin.client.put("/api/telegram/paused", json={"paused": True})).status_code == 200
    async with admin.sm() as s:
        assert (await s.get(AppSetting, "telegram")).value["token_enc"] == "bm90LWEtdmFsaWQtYmxvYg=="
    with respx.mock(assert_all_called=False) as router:
        _mock_ok(router)
        r = await admin.client.put("/api/telegram/config", json={"bot_token": TOKEN})
    assert r.status_code == 200 and r.json()["error"] is None and r.json()["paused"] is True


async def test_detect_chats_dedup_groups_only(admin):
    updates = [
        {"update_id": 1, "message": {"chat": {"id": -5, "type": "group", "title": "Vecchio"}}},
        {"update_id": 2, "message": {"chat": {"id": 77, "type": "private", "first_name": "M"}}},
        {"update_id": 3, "my_chat_member": {"chat": {"id": -100, "type": "supergroup",
                                                      "title": "Super"}}},
        {"update_id": 4, "channel_post": {"chat": {"id": -200, "type": "channel",
                                                    "title": "Canale"}}},
        {"update_id": 5, "message": {"chat": {"id": -5, "type": "group", "title": "Nuovo"}}},
    ]
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE}/getUpdates").mock(return_value=_ok(updates))
        r = await admin.client.post("/api/telegram/detect-chats", json={"bot_token": TOKEN})
        assert "offset" not in json.loads(route.calls.last.request.content)
    assert r.status_code == 200, r.text
    assert r.json() == {"chats": [
        {"id": -5, "type": "group", "title": "Nuovo"},
        {"id": -200, "type": "channel", "title": "Canale"},
        {"id": -100, "type": "supergroup", "title": "Super"},
    ]}
    # without a body token and nothing stored
    r = await admin.client.post("/api/telegram/detect-chats", json={})
    assert r.status_code == 400 and r.json()["details"] == {"field": "bot_token"}
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/getUpdates").mock(return_value=_err(409, "Conflict: webhook"))
        r = await admin.client.post("/api/telegram/detect-chats", json={"bot_token": TOKEN})
    assert r.status_code == 400 and "webhook" in r.json()["message"]


async def test_test_message_sent_even_when_paused(admin):
    from smtrading.telegram.models import TelegramOutbox

    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    assert (await admin.client.put("/api/telegram/paused", json={"paused": True})).status_code == 200
    with respx.mock(assert_all_called=False) as router:
        send = router.post(f"{BASE}/sendMessage").mock(return_value=_ok({"message_id": 9}))
        r = await admin.client.post("/api/telegram/test")
        assert send.called
        assert json.loads(send.calls.last.request.content)["text"] == \
            "🟢 Messaggio di prova da SMTrading"
    assert r.json() == {"ok": True}
    async with admin.sm() as s:
        row = (await s.scalars(select(TelegramOutbox))).one()
    assert (row.category, row.status, row.telegram_message_id) == ("test", "sent", 9)
    assert row.sent_at is not None


async def test_test_message_failure_reports_error(admin):
    from smtrading.telegram.models import TelegramOutbox

    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE}/sendMessage").mock(return_value=_err(403, "Forbidden: kicked"))
        r = await admin.client.post("/api/telegram/test")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "Il bot non è membro del gruppo o è stato rimosso"}
    async with admin.sm() as s:
        row = (await s.scalars(select(TelegramOutbox))).one()
    assert row.status == "failed" and row.last_error.startswith("Il bot non")


async def test_test_message_needs_config(admin):
    r = await admin.client.post("/api/telegram/test")
    assert r.status_code == 400 and r.json()["details"] == {"field": "bot_token"}


async def _outbox(admin, status, n=1, source="system", category="event"):
    from smtrading.telegram.models import TelegramOutbox

    async with admin.sm() as s:
        rows = [TelegramOutbox(source=source, category=category, level="info",
                               text=f"m{i}", status=status) for i in range(n)]
        s.add_all(rows)
        await s.commit()
    return [r.id for r in rows]


async def test_pause_resume_moves_rows(admin):
    await _outbox(admin, "paused", 2)
    await _outbox(admin, "failed", 1)
    r = await admin.client.put("/api/telegram/paused", json={"paused": True})
    assert r.status_code == 200 and r.json()["paused"] is True
    assert r.json()["outbox"] == {"pending": 0, "paused": 2, "failed": 1}
    r = await admin.client.put("/api/telegram/paused", json={"paused": False})
    assert r.json()["paused"] is False
    assert r.json()["outbox"] == {"pending": 2, "paused": 0, "failed": 1}


async def _hold_state(admin):
    """(hold_version, pending rows not due yet)."""
    from sqlalchemy import func

    from smtrading.telegram.config import load_hold_version
    from smtrading.telegram.models import TelegramOutbox

    async with admin.sm() as s:
        version = await load_hold_version(s)
        held = await s.scalar(select(func.count()).select_from(TelegramOutbox).where(
            TelegramOutbox.status == "pending", TelegramOutbox.next_attempt_at > func.now()))
    return version, held


async def _hold_rows(admin, ids):
    from sqlalchemy import text, update

    from smtrading.telegram.models import TelegramOutbox

    async with admin.sm() as s:
        await s.execute(update(TelegramOutbox).where(TelegramOutbox.id.in_(ids))
                        .values(next_attempt_at=text("now() + interval '15 minutes'")))
        await s.commit()


async def test_config_change_releases_sender_hold(admin):
    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    held = await _outbox(admin, "pending", 2)
    await _hold_rows(admin, held)
    version, n = await _hold_state(admin)
    assert n == 2
    with respx.mock(assert_all_called=False) as router:  # same values: no release
        _mock_ok(router)
        await admin.client.put("/api/telegram/config", json={"chat_id": CHAT})
    assert await _hold_state(admin) == (version, 2)
    with respx.mock(assert_all_called=False) as router:  # new token: released
        _mock_ok(router, base=BASE2)
        r = await admin.client.put("/api/telegram/config", json={"bot_token": TOKEN2})
        assert r.status_code == 200, r.text
    assert await _hold_state(admin) == (version + 1, 0)


async def test_resume_releases_sender_hold(admin):
    held = await _outbox(admin, "pending", 1)
    await _hold_rows(admin, held)
    await admin.client.put("/api/telegram/paused", json={"paused": True})
    version, _ = await _hold_state(admin)
    r = await admin.client.put("/api/telegram/paused", json={"paused": False})
    assert r.status_code == 200
    assert await _hold_state(admin) == (version + 1, 0)


async def test_outbox_failed_count_excludes_test_messages(admin):
    await _outbox(admin, "failed", 1, category="test")
    await _outbox(admin, "failed", 2)
    r = await admin.client.get("/api/telegram")
    assert r.json()["outbox"]["failed"] == 2


async def test_retry_all_failed(admin):
    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)
    failed = await _outbox(admin, "failed", 2)
    [test_row] = await _outbox(admin, "failed", 1, category="test")
    [sent] = await _outbox(admin, "sent", 1)
    version, _ = await _hold_state(admin)
    r = await admin.client.post("/api/telegram/outbox/retry-failed")
    assert r.status_code == 200 and r.json() == {"retried": 2}
    items = {i["id"]: i for i in (await admin.client.get("/api/telegram/outbox")).json()["items"]}
    assert [items[i]["status"] for i in failed] == ["pending", "pending"]
    assert items[test_row]["status"] == "failed" and items[sent]["status"] == "sent"
    assert (await _hold_state(admin))[0] == version + 1
    assert ("admin:telegram.retry_failed", {"by": admin.email, "retried": 2}) in \
        await _audits(admin)


async def test_outbox_list_and_retry(admin):
    from smtrading.agents import service as agents

    async with admin.sm() as s:
        agent, _ = await agents.create_agent(s, "Notifier")
        await s.commit()
    sent = await _outbox(admin, "sent", 2)
    failed = await _outbox(admin, "failed", 1, source=f"agent:{agent.id}", category="agent")
    c = admin.client
    items = (await c.get("/api/telegram/outbox")).json()["items"]
    assert [i["id"] for i in items] == [failed[0], sent[1], sent[0]]
    assert items[0]["agent"] == "Notifier" and items[1]["agent"] is None
    assert set(items[0]) == {"id", "source", "agent", "category", "level", "text", "status",
                             "attempts", "last_error", "created_at", "sent_at"}
    assert [i["id"] for i in (await c.get("/api/telegram/outbox?status=failed")).json()
            ["items"]] == failed
    assert [i["id"] for i in (await c.get(f"/api/telegram/outbox?before_id={sent[1]}"))
            .json()["items"]] == [sent[0]]
    assert len((await c.get("/api/telegram/outbox?limit=1")).json()["items"]) == 1
    assert (await c.get("/api/telegram/outbox?category=agent")).json()["items"][0]["id"] == \
        failed[0]
    assert (await c.get("/api/telegram/outbox?status=nope")).status_code == 400
    assert (await c.get("/api/telegram/outbox?limit=201")).status_code == 400

    r = await c.post(f"/api/telegram/outbox/{failed[0]}/retry")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert r.json()["attempts"] == 0
    assert (await c.post(f"/api/telegram/outbox/{sent[0]}/retry")).status_code == 400
    assert (await c.post("/api/telegram/outbox/999999/retry")).status_code == 404


async def test_decisions_filters(admin):
    from smtrading.activity.service import log_decision
    from smtrading.agents import service as agents
    from smtrading.portfolios.models import Portfolio

    async with admin.sm() as s:
        a1, _ = await agents.create_agent(s, "Alpha")
        a2, _ = await agents.create_agent(s, "Beta")
        pid = (await s.scalars(select(Portfolio.id))).first()
        await agents.assign_portfolio(s, a1.id, pid, "read")
        d1 = await log_decision(s, a1.id, kind="proposal", summary="compra", portfolio_id=pid,
                                symbols=["AAPL"])
        d2 = await log_decision(s, a2.id, kind="veto", summary="no")
        d3 = await log_decision(s, a1.id, kind="veto", summary="ancora no")
        await s.commit()
    c = admin.client
    items = (await c.get("/api/decisions")).json()["items"]
    assert [i["id"] for i in items] == [d3.id, d2.id, d1.id]
    assert items[2]["agent"] == "Alpha" and items[2]["portfolio"] is not None
    assert items[2]["symbols"] == ["AAPL"] and items[1]["portfolio"] is None
    assert [i["id"] for i in (await c.get(f"/api/decisions?agent_id={a1.id}")).json()
            ["items"]] == [d3.id, d1.id]
    assert [i["id"] for i in (await c.get("/api/decisions?kind=veto")).json()["items"]] == \
        [d3.id, d2.id]
    assert [i["id"] for i in (await c.get(f"/api/decisions?portfolio_id={pid}")).json()
            ["items"]] == [d1.id]
    assert [i["id"] for i in (await c.get(f"/api/decisions?before_id={d2.id}")).json()
            ["items"]] == [d1.id]
    assert (await c.get("/api/decisions?kind=loud")).status_code == 400


async def test_patch_agent_can_notify_audited(admin):
    c = admin.client
    agent = (await c.post("/api/agents", json={"name": "Notifier"})).json()["agent"]
    assert agent["can_notify"] is False
    r = await c.patch(f"/api/agents/{agent['id']}", json={"can_notify": True})
    assert r.status_code == 200 and r.json()["can_notify"] is True
    assert r.json()["active"] is True  # untouched
    r = await c.patch(f"/api/agents/{agent['id']}", json={"active": False})
    assert r.json()["active"] is False and r.json()["can_notify"] is True
    assert (await c.patch(f"/api/agents/{agent['id']}", json={"can_notify": None})).status_code \
        == 400
    audits = [p for m, p in await _audits(admin) if m == "admin:agent.notify"]
    assert audits == [{"agent_id": agent["id"], "agent": "Notifier", "can_notify": True,
                       "by": admin.email}]


async def test_every_change_audited(admin):
    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router, enabled=True)
        router.post(f"{BASE}/sendMessage").mock(return_value=_ok({"message_id": 1}))
        await admin.client.post("/api/telegram/test")
    await admin.client.put("/api/telegram/paused", json={"paused": True})
    failed = await _outbox(admin, "failed")
    await admin.client.post(f"/api/telegram/outbox/{failed[0]}/retry")
    audits = await _audits(admin, "admin:telegram.")
    assert [m for m, _ in audits] == ["admin:telegram.config", "admin:telegram.test",
                                      "admin:telegram.pause", "admin:telegram.retry"]
    assert audits[0][1] == {"by": admin.email, "changed": ["bot_token", "chat_id", "enabled"],
                            "chat_id": CHAT, "enabled": True}
    _no_token(json.dumps(audits))


async def test_routes_require_login(admin):
    async with httpx.AsyncClient(transport=admin.client._transport,
                                 base_url="http://test") as anon:
        for method, path in (("GET", "/api/telegram"), ("PUT", "/api/telegram/config"),
                             ("GET", "/api/telegram/outbox"), ("GET", "/api/decisions")):
            assert (await anon.request(method, path, json={})).status_code == 401


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/telegram/config", {"enabled": False}),
    ("POST", "/api/telegram/detect-chats", {}),
    ("POST", "/api/telegram/test", None),
    ("PUT", "/api/telegram/paused", {"paused": True}),
    ("POST", "/api/telegram/outbox/1/retry", None),
    ("POST", "/api/telegram/outbox/retry-failed", None),
])
async def test_mutating_routes_require_csrf(admin, method, path, body):
    async with httpx.AsyncClient(transport=admin.client._transport, base_url="http://test",
                                 cookies=admin.client.cookies) as no_csrf:
        r = await no_csrf.request(method, path, json=body)
    assert r.status_code == 403, r.text


async def test_invalid_filter_messages_list_values(admin):
    r = await admin.client.get("/api/telegram/outbox?status=nope")
    assert r.status_code == 400
    assert r.json()["message"].endswith("pending, sent, failed, paused, skipped")
    r = await admin.client.get("/api/decisions?kind=loud")
    assert r.status_code == 400
    assert r.json()["message"].endswith(
        "proposal, approval, veto, allocation, strategy, risk_rule, other")
    assert "<" not in r.json()["message"] and "Kind" not in r.json()["message"]


async def test_token_change_keeps_concurrent_chat_migration(admin):
    """A supergroup migration saved while the new token is being verified is not reverted."""
    from smtrading.risk.models import AppSetting
    from smtrading.telegram.config import load_config

    with respx.mock(assert_all_called=False) as router:
        await _configure(admin, router)

    async def migrate_then_answer(request):
        async with admin.sm() as s:
            row = await s.get(AppSetting, "telegram")
            row.value = {**row.value, "chat_id": "-100999", "chat_title": "Migrato"}
            await s.commit()
        return _ok({"id": 2, "username": "smt_bot2"})

    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE2}/getMe").mock(side_effect=migrate_then_answer)
        router.post(f"{BASE2}/getChat").mock(
            return_value=_ok({"id": int(CHAT), "type": "supergroup", "title": "Trading"}))
        r = await admin.client.put("/api/telegram/config", json={"bot_token": TOKEN2})
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == "-100999" and r.json()["chat_title"] == "Migrato"
    async with admin.sm() as s:
        cfg = await load_config(s)
    assert cfg.bot_token == TOKEN2 and cfg.chat_id == "-100999"


# --- CLI ---------------------------------------------------------------------------------

async def _cli(admin, monkeypatch, argv, stdin=""):
    from redis.asyncio import Redis

    from smtrading import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    redis = Redis.from_url(admin.redis_url)
    try:
        return await cli.run_command(cli.build_parser().parse_args(argv), admin.sm, None, redis)
    finally:
        await redis.aclose()


async def test_cli_telegram_set_reads_token_from_stdin(admin, monkeypatch, caplog):
    from smtrading.telegram.config import load_config

    caplog.set_level(logging.DEBUG)

    with respx.mock(assert_all_called=False) as router:
        me, _ = _mock_ok(router)
        code, out = await _cli(admin, monkeypatch,
                               ["telegram", "set", "--chat-id", CHAT, "--token-stdin", "--enable"],
                               stdin=TOKEN + "\n")
        assert me.called
    assert code == 0, out
    assert json.loads(out) == {"bot_username": "@smt_bot", "chat_id": CHAT,
                               "chat_title": "Trading", "enabled": True, "paused": False}
    _no_token(out)
    _no_token(caplog.text)
    async with admin.sm() as s:
        assert (await load_config(s)).bot_token == TOKEN
    # a bad token: error JSON without the token, nothing saved
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE2}/getMe").mock(return_value=_err(401, "Unauthorized"))
        code, out = await _cli(admin, monkeypatch,
                               ["telegram", "set", "--chat-id", CHAT, "--token-stdin"],
                               stdin=TOKEN2)
    assert code == 1 and json.loads(out)["error"]["message"] == "Token del bot non valido"
    _no_token(out)
    async with admin.sm() as s:
        assert (await load_config(s)).bot_token == TOKEN
    # --no-verify: no request at all
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=_ok({}))
        code, out = await _cli(admin, monkeypatch,
                               ["telegram", "set", "--chat-id", "-777", "--no-verify"])
        assert not route.called
    assert code == 0 and json.loads(out)["chat_id"] == "-777"
    assert json.loads(out)["chat_title"] is None


def test_cli_token_never_from_argv():
    from smtrading import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["telegram", "set", "--chat-id", CHAT, "--token", TOKEN])


async def test_cli_agent_notify(admin, monkeypatch):
    from smtrading.agents import service as agents

    async with admin.sm() as s:
        await agents.create_agent(s, "Notifier")
        await s.commit()
    code, out = await _cli(admin, monkeypatch, ["agent", "notify", "Notifier", "on"])
    assert code == 0 and json.loads(out)["can_notify"] is True
    code, out = await _cli(admin, monkeypatch, ["agent", "notify", "Notifier", "off"])
    assert json.loads(out)["can_notify"] is False
    audits = [p["can_notify"] for m, p in await _audits(admin) if m == "admin:agent.notify"]
    assert audits == [True, False]
    code, _ = await _cli(admin, monkeypatch, ["agent", "notify", "Nessuno", "on"])
    assert code == 1
