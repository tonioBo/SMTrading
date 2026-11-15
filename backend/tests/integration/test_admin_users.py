"""Portal users: roles (`admin`, `viewer`), temporary passwords with the forced change,
the /api/users management routes with their protections, the role coverage of every
route, the Telegram security notices and the `users` CLI."""

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime

import httpx
import pyotp
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.models import McpAuditLog
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox

PASSWORD = "correct-horse-battery"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# unsafe routes a viewer may call (the spec's explicit exceptions); everything else is
# refused with 403 FORBIDDEN
VIEWER_UNSAFE_EXCEPTIONS = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/totp/setup"),
    ("POST", "/api/auth/totp/enable"),
    ("POST", "/api/auth/totp/disable"),
    ("POST", "/api/live-links/{link_id}/disarm"),
    ("POST", "/api/kill-switch"),  # only towards "on" (tested below)
}
# reads only an admin may make
ADMIN_ONLY_READS = {("GET", "/api/users")}
SKIPPED_PATHS = {"/health", "/mcp", "/mcp/"}


def _app_routes() -> list[tuple[str, str]]:
    from smtrading.api.main import create_app

    out = []
    for ctx in iter_route_contexts(create_app().routes):
        if not isinstance(ctx.original_route, APIRoute) or ctx.path in SKIPPED_PATHS:
            continue
        out.extend((m, ctx.path) for m in ctx.methods if m != "HEAD")
    return sorted(out)


def _fill(path: str) -> str:
    def value(m: re.Match) -> str:
        name = m.group(1)
        if name in ("halt_id", "outbox_id", "version"):
            return "999999"
        if name in ("slug", "name", "symbol"):
            return "zz-unknown"
        return str(uuid.uuid4())

    return re.sub(r"\{([^}]+)\}", value, path)


class Env:
    def __init__(self, sm, transport, redis_url):
        self.sm, self.transport, self.redis_url = sm, transport, redis_url
        self.clients: list[httpx.AsyncClient] = []

    async def client(self, email: str, password: str = PASSWORD, totp: str | None = None,
                     *, expect: int = 200) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        self.clients.append(c)
        body = {"email": email, "password": password}
        if totp:
            body["totp"] = totp
        r = await c.post("/api/auth/login", json=body)
        assert r.status_code == expect, r.text
        if expect == 200:
            c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
        return c

    async def add_user(self, role: str = "admin", *, totp_secret: str | None = None,
                       must_change: bool = False) -> tuple[str, uuid.UUID]:
        from smtrading.admin.auth import hash_password
        from smtrading.admin.models import AdminUser

        email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
        async with self.sm() as s:
            user = AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                             role=role, must_change_password=must_change)
            if totp_secret:
                user.totp_enabled = True
                user.totp_secret_encrypted = encrypt_json(
                    {"secret": totp_secret}, get_settings().master_key)
            s.add(user)
            await s.commit()
        return email, user.id

    async def code(self, secret: str) -> str:
        r = Redis.from_url(self.redis_url)
        try:
            keys = [k async for k in r.scan_iter("adm:totp_used:*")]
            if keys:
                await r.delete(*keys)
        finally:
            await r.aclose()
        return pyotp.TOTP(secret).now()

    async def user(self, user_id):
        from smtrading.admin.models import AdminUser

        async with self.sm() as s:
            return await s.get(AdminUser, user_id)


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm, redis: McpRuntime(sm, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    engine = make_engine(fresh_db)
    e = Env(async_sessionmaker(engine, expire_on_commit=False),
            httpx.ASGITransport(app=app), redis_url)
    try:
        yield e
    finally:
        for c in e.clients:
            await c.aclose()
        stop.set()
        await lifespan
        await engine.dispose()


async def _telegram(sm) -> None:
    async with sm() as s:
        await save_config(s, TelegramConfig(bot_token="123456789:AAH" + "x" * 32,
                                            chat_id="-100123", enabled=True))
        await s.commit()


async def _outbox_texts(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text).where(
            TelegramOutbox.source == "system").order_by(TelegramOutbox.id)))


async def _audit(sm, prefix="admin:user.") -> list[McpAuditLog]:
    async with sm() as s:
        return list(await s.scalars(select(McpAuditLog).where(
            McpAuditLog.method.startswith(prefix)).order_by(McpAuditLog.id)))


# -- /api/auth/me and the viewer -----------------------------------------------------

async def test_me_exposes_role_and_must_change_password(env):
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    me = (await c.get("/api/auth/me")).json()
    assert me == {"email": email, "totp_enabled": False, "role": "viewer",
                  "must_change_password": False}


async def test_existing_users_become_admins(env):
    """The migration's default: a row written without a role is an admin."""
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser

    async with env.sm() as s:
        user = AdminUser(email="legacy@example.com", password_hash=await hash_password(PASSWORD))
        s.add(user)
        await s.commit()
        await s.refresh(user)
        assert user.role == "admin" and user.must_change_password is False
        assert user.disabled_at is None


async def test_viewer_reads_but_cannot_write(env):
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    assert (await c.get("/api/portfolios")).status_code == 200
    r = await c.post("/api/portfolios", json={"name": "X", "initial_cash": "100"})
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"
    assert "sola lettura" in r.json()["message"]


def test_exceptions_are_real_routes():
    routes = set(_app_routes())
    assert VIEWER_UNSAFE_EXCEPTIONS <= routes
    assert ADMIN_ONLY_READS <= routes


async def test_every_unsafe_route_refuses_the_viewer(env):
    """Enumerates ALL the app's routes: a new unsafe route forgotten by the role rules
    fails here."""
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    checked = 0
    for method, path in _app_routes():
        if method not in UNSAFE or (method, path) in VIEWER_UNSAFE_EXCEPTIONS:
            continue
        r = await c.request(method, _fill(path), json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["code"] == "FORBIDDEN", (method, path)
        checked += 1
    assert checked > 50


async def test_every_read_accepts_the_viewer(env):
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    for method, path in _app_routes():
        if method != "GET" or (method, path) in ADMIN_ONLY_READS:
            continue
        r = await c.get(_fill(path))
        assert r.status_code not in (401, 403), (path, r.status_code, r.text)
    for method, path in ADMIN_ONLY_READS:
        r = await c.request(method, _fill(path))
        assert r.status_code == 403, path


async def test_viewer_emergency_actions(env):
    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    # disarm: allowed (an unknown link is a 404, not a 403)
    r = await c.post(f"/api/live-links/{uuid.uuid4()}/disarm")
    assert r.status_code == 404, r.text
    # kill switch: only towards "on"
    r = await c.post("/api/kill-switch", json={"on": True})
    assert r.status_code == 200 and r.json()["on"] is True
    r = await c.post("/api/kill-switch", json={"on": False})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert (await c.get("/api/kill-switch")).json() == {"on": True}
    admin_email, _ = await env.add_user("admin")
    a = await env.client(admin_email)
    assert (await a.post("/api/kill-switch", json={"on": False})).status_code == 200
    # own TOTP and password
    assert (await c.post("/api/auth/totp/setup")).status_code == 200
    r = await c.post("/api/auth/password", json={"current": PASSWORD, "new": "another-password-1"})
    assert r.status_code == 200
    c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
    # but not the other sessions' revocation, nor the users
    assert (await c.post("/api/auth/sessions/revoke-others")).status_code == 403
    assert (await c.post("/api/auth/logout")).status_code == 204


# -- temporary passwords and the forced change ------------------------------------------

async def test_create_user_temp_password_once_and_forced_change(env, caplog):
    caplog.set_level(logging.DEBUG)
    admin_email, _ = await env.add_user("admin")
    a = await env.client(admin_email)
    r = await a.post("/api/users", json={"email": "  New.Viewer@Example.com ", "role": "viewer"})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    temp = body["temp_password"]
    assert len(temp) >= 16
    user = body["user"]
    assert user["email"] == "new.viewer@example.com" and user["role"] == "viewer"
    assert user["must_change_password"] is True and user["active"] is True
    assert user["created_by"] == admin_email
    # never shown again, never in clear in the database, the audit or the logs
    listed = (await a.get("/api/users")).json()
    assert temp not in json.dumps(listed)
    row = await env.user(uuid.UUID(user["id"]))
    assert temp not in row.password_hash
    audit = await _audit(env.sm)
    assert [x.method for x in audit] == ["admin:user.create"]
    assert temp not in json.dumps(audit[0].params)
    assert all(temp not in rec.getMessage() for rec in caplog.records)
    from smtrading.common.logsetup import redact_log_text

    assert temp not in redact_log_text(f"oops {temp}")

    v = await env.client("new.viewer@example.com", temp)
    me = (await v.get("/api/auth/me")).json()
    assert me["must_change_password"] is True and me["role"] == "viewer"
    for method, path in (("GET", "/api/portfolios"), ("POST", "/api/auth/totp/setup"),
                         ("GET", "/api/kill-switch")):
        r = await v.request(method, path)
        assert r.status_code == 403, path
        assert r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
        assert "password" in r.json()["message"]
    same = await v.post("/api/auth/password", json={"current": temp, "new": temp})
    assert same.status_code == 400
    r = await v.post("/api/auth/password", json={"current": temp, "new": "my-own-password-1"})
    assert r.status_code == 200, r.text
    v.headers["X-CSRF-Token"] = v.cookies["smt_csrf"]
    assert (await v.get("/api/auth/me")).json()["must_change_password"] is False
    assert (await v.get("/api/portfolios")).status_code == 200


async def test_create_user_validation(env):
    admin_email, _ = await env.add_user("admin")
    a = await env.client(admin_email)
    for body in ({"email": "not-an-email", "role": "viewer"},
                 {"email": "x@example.com", "role": "owner"},
                 {"email": admin_email.upper(), "role": "viewer"}):
        r = await a.post("/api/users", json=body)
        assert r.status_code == 400, (body, r.text)
    viewer_email, _ = await env.add_user("viewer")
    v = await env.client(viewer_email)
    r = await v.post("/api/users", json={"email": "y@example.com", "role": "viewer"})
    assert r.status_code == 403


# -- protections -----------------------------------------------------------------------

async def test_no_self_role_change_disable_or_reset(env):
    admin_email, admin_id = await env.add_user("admin")
    await env.add_user("admin")
    a = await env.client(admin_email)
    for method, path, body in (
        ("PUT", f"/api/users/{admin_id}/role", {"role": "viewer"}),
        ("POST", f"/api/users/{admin_id}/disable", None),
        ("POST", f"/api/users/{admin_id}/reset-password", {}),
        ("POST", f"/api/users/{admin_id}/reset-totp", None),
    ):
        r = await a.request(method, path, json=body)
        assert r.status_code == 403, (path, r.text)
    assert (await env.user(admin_id)).role == "admin"


async def test_disable_kills_sessions_and_login_then_enable(env):
    admin_email, _ = await env.add_user("admin")
    viewer_email, viewer_id = await env.add_user("viewer")
    a = await env.client(admin_email)
    v = await env.client(viewer_email)
    r = await a.post(f"/api/users/{viewer_id}/disable")
    assert r.status_code == 200 and r.json()["user"]["active"] is False
    assert (await v.get("/api/auth/me")).status_code == 401
    r = await env.client(viewer_email, expect=403)
    assert (await env.user(viewer_id)).disabled_at is not None
    r = await a.post(f"/api/users/{viewer_id}/enable")
    assert r.status_code == 200 and r.json()["user"]["active"] is True
    await env.client(viewer_email)
    assert [x.method for x in await _audit(env.sm)] == [
        "admin:user.disable", "admin:user.enable"]


async def test_disabled_user_session_dies_even_without_revocation(env):
    """A disabled user's surviving session is refused (and destroyed) at once."""
    viewer_email, viewer_id = await env.add_user("viewer")
    v = await env.client(viewer_email)
    from smtrading.admin.models import AdminUser

    async with env.sm() as s:
        (await s.get(AdminUser, viewer_id)).disabled_at = datetime.now(UTC)
        await s.commit()
    assert (await v.get("/api/auth/me")).status_code == 401


async def test_role_change_revokes_sessions(env):
    admin_email, _ = await env.add_user("admin")
    viewer_email, viewer_id = await env.add_user("viewer")
    a = await env.client(admin_email)
    v = await env.client(viewer_email)
    r = await a.put(f"/api/users/{viewer_id}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert (await v.get("/api/auth/me")).status_code == 401
    v2 = await env.client(viewer_email)
    assert (await v2.get("/api/auth/me")).json()["role"] == "admin"


async def test_reset_password_and_totp(env):
    admin_email, _ = await env.add_user("admin")
    secret = pyotp.random_base32()
    viewer_email, viewer_id = await env.add_user("viewer", totp_secret=secret)
    a = await env.client(admin_email)
    v = await env.client(viewer_email, totp=await env.code(secret))
    r = await a.post(f"/api/users/{viewer_id}/reset-totp")
    assert r.status_code == 200 and r.json()["user"]["totp_enabled"] is False
    assert (await v.get("/api/auth/me")).status_code == 401
    v = await env.client(viewer_email)  # no TOTP needed any more
    r = await a.post(f"/api/users/{viewer_id}/reset-password", json={})
    assert r.status_code == 200, r.text
    temp = r.json()["temp_password"]
    assert len(temp) >= 16 and r.json()["user"]["must_change_password"] is True
    assert (await v.get("/api/auth/me")).status_code == 401
    await env.client(viewer_email, expect=403)  # the old password is gone
    v = await env.client(viewer_email, temp)
    assert (await v.get("/api/portfolios")).json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert [x.method for x in await _audit(env.sm)] == [
        "admin:user.reset_totp", "admin:user.reset_password"]


async def test_unknown_user_is_404(env):
    admin_email, _ = await env.add_user("admin")
    a = await env.client(admin_email)
    r = await a.post(f"/api/users/{uuid.uuid4()}/disable")
    assert r.status_code == 404


async def test_admin_actions_need_the_actor_totp(env):
    secret = pyotp.random_base32()
    boss_email, _ = await env.add_user("admin", totp_secret=secret)
    other_admin_email, other_admin_id = await env.add_user("admin")
    _viewer_email, viewer_id = await env.add_user("viewer")
    a = await env.client(boss_email, totp=await env.code(secret))
    # a viewer needs no TOTP
    r = await a.post("/api/users", json={"email": "v2@example.com", "role": "viewer"})
    assert r.status_code == 200
    for method, path, body in (
        ("POST", "/api/users", {"email": "a2@example.com", "role": "admin"}),
        ("PUT", f"/api/users/{viewer_id}/role", {"role": "admin"}),
        ("POST", f"/api/users/{other_admin_id}/reset-password", {}),
    ):
        r = await a.request(method, path, json=body)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["details"] == {"field": "totp", "totp_required": True}
        wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
        r = await a.request(method, path, json={**body, "totp": wrong})
        assert r.status_code == 403 and r.json()["details"]["field"] == "totp"
        r = await a.request(method, path, json={**body, "totp": await env.code(secret)})
        assert r.status_code == 200, (path, r.text)
    # an actor without TOTP: no step-up
    b = await env.client(other_admin_email, (r.json()["temp_password"]))
    r = await b.post("/api/auth/password", json={"current": r.json()["temp_password"],
                                                 "new": "other-admin-password"})
    assert r.status_code == 200
    b.headers["X-CSRF-Token"] = b.cookies["smt_csrf"]
    r = await b.post("/api/users", json={"email": "a3@example.com", "role": "admin"})
    assert r.status_code == 200, r.text


async def test_user_totp_failures_lock_out(env):
    secret = pyotp.random_base32()
    boss_email, _boss_id = await env.add_user("admin", totp_secret=secret)
    a = await env.client(boss_email, totp=await env.code(secret))
    wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    body = {"email": "a9@example.com", "role": "admin"}
    for _ in range(5):
        r = await a.post("/api/users", json={**body, "totp": wrong})
        assert r.status_code == 403
    r = await a.post("/api/users", json={**body, "totp": await env.code(secret)})
    assert r.status_code == 429
    assert r.json() == {"code": "RATE_LIMITED",
                        "message": "troppi codici TOTP errati, riprova tra 15 minuti",
                        "details": {"field": "totp"}}


async def test_last_active_admin_is_kept_under_concurrency(env):
    """Two admins demoting each other at once: the lock lets exactly one through."""
    from smtrading.admin import users
    from smtrading.admin.models import AdminUser
    from smtrading.common.errors import SMTError

    _a_email, a_id = await env.add_user("admin")
    _b_email, b_id = await env.add_user("admin")

    async def demote(actor_id, target_id):
        async with env.sm() as s:
            actor = await s.get(AdminUser, actor_id)
            try:
                await users.set_role(s, target_id, "viewer", actor=actor.email,
                                     actor_id=actor.id)
                await s.commit()
                return "ok"
            except SMTError as e:
                await s.rollback()
                return e.message

    results = await asyncio.gather(demote(a_id, b_id), demote(b_id, a_id))
    assert sorted(results) == ["deve restare almeno un amministratore attivo", "ok"]
    async with env.sm() as s:
        assert await s.scalar(select(func.count()).select_from(AdminUser).where(
            AdminUser.role == "admin", AdminUser.disabled_at.is_(None))) == 1


async def test_cannot_disable_the_last_active_admin_from_the_cli(env):
    a_email, _a_id = await env.add_user("admin")
    code, out = await _cli(env, "users", "disable", a_email)
    assert code == 1 and "almeno un amministratore" in out
    code, out = await _cli(env, "users", "set-role", a_email, "viewer")
    assert code == 1 and "almeno un amministratore" in out


# -- Telegram --------------------------------------------------------------------------

async def test_security_notices_on_admin_create_promote_reactivate(env):
    await _telegram(env.sm)
    admin_email, _ = await env.add_user("admin")
    viewer_email, viewer_id = await env.add_user("viewer")
    a = await env.client(admin_email)
    r = await a.post("/api/users", json={"email": "boss2@example.com", "role": "admin"})
    temp = r.json()["temp_password"]
    boss2 = r.json()["user"]["id"]
    await a.post("/api/users", json={"email": "viewer2@example.com", "role": "viewer"})
    await a.put(f"/api/users/{viewer_id}/role", json={"role": "admin"})
    await a.post(f"/api/users/{boss2}/disable")
    await a.post(f"/api/users/{boss2}/enable")
    texts = await _outbox_texts(env.sm)
    assert len(texts) == 3, texts
    assert "boss2@example.com" in texts[0] and admin_email in texts[0]
    assert viewer_email in texts[1]
    assert "boss2@example.com" in texts[2]
    assert all(temp not in t for t in texts)
    assert "viewer2@example.com" not in " ".join(texts)


# -- CLI -------------------------------------------------------------------------------

async def _cli(env, *argv):
    from smtrading import cli

    redis = Redis.from_url(env.redis_url)
    try:
        return await cli.run_command(cli.build_parser().parse_args(list(argv)), env.sm,
                                     None, redis)
    finally:
        await redis.aclose()


async def test_cli_users(env):
    code, out = await _cli(env, "users", "create", "Rescue@Example.com", "--role", "admin")
    assert code == 0, out
    created = json.loads(out)
    temp = created["temp_password"]
    assert len(temp) >= 16 and created["email"] == "rescue@example.com"
    assert created["role"] == "admin"
    c = await env.client("rescue@example.com", temp)
    assert (await c.get("/api/auth/me")).json()["must_change_password"] is True

    code, out = await _cli(env, "users", "create", "watch@example.com")
    assert code == 0 and json.loads(out)["role"] == "viewer"
    code, out = await _cli(env, "users", "list")
    rows = json.loads(out)
    assert [r["email"] for r in rows] == ["rescue@example.com", "watch@example.com"]
    assert temp not in out
    assert all(not {"temp_password", "password_hash"} & set(r) for r in rows)

    code, out = await _cli(env, "users", "set-role", "watch@example.com", "admin")
    assert code == 0 and json.loads(out)["role"] == "admin"
    code, out = await _cli(env, "users", "disable", "watch@example.com")
    assert code == 0 and json.loads(out)["active"] is False
    code, out = await _cli(env, "users", "reset-password", "rescue@example.com")
    new_temp = json.loads(out)["temp_password"]
    assert code == 0 and new_temp != temp
    assert (await c.get("/api/auth/me")).status_code == 401  # sessions revoked
    await env.client("rescue@example.com", new_temp)
    code, out = await _cli(env, "users", "disable", "nobody@example.com")
    assert code == 1 and json.loads(out)["error"]["code"] == "NOT_FOUND"
    methods = [x.method for x in await _audit(env.sm)]
    assert methods == ["admin:user.create", "admin:user.create", "admin:user.set_role",
                       "admin:user.disable", "admin:user.reset_password"]
    assert all(x.params["by"] == "cli" for x in await _audit(env.sm))

