import asyncio
import re
import uuid

import httpx
import pyotp
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

LOGIN_PATH = "/api/auth/login"
PASSWORD = "correct-horse-battery"


def _protected_routes() -> list[tuple[str, str]]:
    from smtrading.api.main import create_app

    # iter_route_contexts flattens included routers (paths carry the router prefix)
    return sorted(
        (method, ctx.path)
        for ctx in iter_route_contexts(create_app().routes)
        if isinstance(ctx.original_route, APIRoute)
        and ctx.path.startswith("/api/")
        and ctx.path != LOGIN_PATH
        for method in ctx.methods
    )


@pytest.fixture
async def admin_app(migrated_db, redis_url, monkeypatch):
    """(client, sessionmaker) against an app with its lifespan running."""
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime

    monkeypatch.setenv("SMT_DATABASE_URL", migrated_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm, redis: McpRuntime(sm, redis, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():  # entered and exited in one task (anyio cancel scopes)
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    engine = make_engine(migrated_db)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


@pytest.fixture
async def admin_user(admin_app):
    """A fresh admin (unique email) committed to the shared migrated db."""
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser

    _, sm = admin_app
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        user = AdminUser(email=email, password_hash=await hash_password(PASSWORD))
        s.add(user)
        await s.commit()
    return user


async def _login(client, email, password=PASSWORD, **extra):
    return await client.post(LOGIN_PATH, json={"email": email, "password": password, **extra})


def _csrf(client) -> dict:
    return {"X-CSRF-Token": client.cookies["smt_csrf"]}


async def test_login_then_me(admin_app, admin_user):
    client, sm = admin_app
    r = await _login(client, admin_user.email.upper())  # email is case-insensitive
    assert r.status_code == 200
    assert r.json() == {"email": admin_user.email, "totp_enabled": False}
    set_cookie = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("smt_session="))
    csrf_cookie = next(c for c in set_cookie if c.startswith("smt_csrf="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "HttpOnly" not in csrf_cookie

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": admin_user.email, "totp_enabled": False}

    from smtrading.admin.models import AdminUser

    async with sm() as s:
        user = await s.get(AdminUser, admin_user.id)
        assert user.last_login_at is not None


async def test_session_ttl_is_sliding(admin_app, admin_user, redis_url):
    from redis.asyncio import Redis

    client, _ = admin_app
    await _login(client, admin_user.email)
    key = f"adm:sess:{client.cookies['smt_session']}"
    redis = Redis.from_url(redis_url)
    try:
        assert 0 < await redis.ttl(key) <= 12 * 3600
        await redis.expire(key, 60)
        assert (await client.get("/api/auth/me")).status_code == 200
        assert await redis.ttl(key) > 12 * 3600 - 60
    finally:
        await redis.aclose()


async def test_wrong_credentials_then_rate_limit(admin_app, admin_user):
    client, _ = admin_app
    unknown = await _login(client, "nobody@example.com", "whatever-pass")
    assert unknown.status_code == 403
    assert unknown.json() == {
        "code": "FORBIDDEN", "message": "credenziali non valide", "details": None,
    }
    for _ in range(5):
        r = await _login(client, admin_user.email, "wrong-password")
        assert r.status_code == 403
        assert r.json() == unknown.json()  # same message for wrong email and password
    limited = await _login(client, admin_user.email)  # even the right password is refused
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"
    assert "smt_session" not in client.cookies


async def test_totp_flow(admin_app, admin_user):
    client, _ = admin_app
    await _login(client, admin_user.email)
    setup = await client.post("/api/auth/totp/setup", headers=_csrf(client))
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert "issuer=SMTrading" in setup.json()["otpauth_uri"]
    totp = pyotp.TOTP(secret)
    assert (await client.get("/api/auth/me")).json()["totp_enabled"] is False  # not active yet

    bad = await client.post("/api/auth/totp/enable", json={"code": "000000"}, headers=_csrf(client))
    assert bad.status_code == 403
    ok = await client.post("/api/auth/totp/enable", json={"code": totp.now()}, headers=_csrf(client))
    assert ok.status_code == 200 and ok.json()["totp_enabled"] is True

    await client.post("/api/auth/logout", headers=_csrf(client))
    client.cookies.clear()
    missing = await _login(client, admin_user.email)
    assert missing.status_code == 403
    assert missing.json()["details"] == {"totp_required": True}
    wrong = await _login(client, admin_user.email, totp="123456" if totp.now() != "123456" else "654321")
    assert wrong.status_code == 403 and wrong.json()["message"] == "codice TOTP non valido"
    good = await _login(client, admin_user.email, totp=totp.now())
    assert good.status_code == 200 and good.json()["totp_enabled"] is True

    off = await client.post(
        "/api/auth/totp/disable", json={"code": totp.now()}, headers=_csrf(client)
    )
    assert off.status_code == 200 and off.json()["totp_enabled"] is False
    client.cookies.clear()
    assert (await _login(client, admin_user.email)).status_code == 200


async def test_totp_secret_is_encrypted(admin_app, admin_user):
    from smtrading.admin.models import AdminUser

    client, sm = admin_app
    await _login(client, admin_user.email)
    secret = (await client.post("/api/auth/totp/setup", headers=_csrf(client))).json()["secret"]
    async with sm() as s:
        stored = (await s.get(AdminUser, admin_user.id)).totp_secret_encrypted
    assert stored is not None and secret.encode() not in stored


async def test_post_without_csrf_is_forbidden(admin_app, admin_user):
    client, _ = admin_app
    await _login(client, admin_user.email)
    for headers in ({}, {"X-CSRF-Token": "nope"}):
        r = await client.post("/api/auth/totp/setup", headers=headers)
        assert r.status_code == 403
        assert r.json() == {"code": "FORBIDDEN", "message": "CSRF", "details": None}
    assert (await client.post("/api/auth/totp/setup", headers=_csrf(client))).status_code == 200


async def test_logout_invalidates_session(admin_app, admin_user):
    client, _ = admin_app
    await _login(client, admin_user.email)
    sid = client.cookies["smt_session"]
    csrf = client.cookies["smt_csrf"]
    r = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    client.cookies.clear()
    client.cookies.set("smt_session", sid)  # replaying the old cookie does not work
    me = await client.get("/api/auth/me")
    assert me.status_code == 401
    assert me.json() == {"code": "FORBIDDEN", "message": "login richiesto", "details": None}


async def test_change_password(admin_app, admin_user):
    client, _ = admin_app
    await _login(client, admin_user.email)
    short = await client.post(
        "/api/auth/password", json={"current": PASSWORD, "new": "short"}, headers=_csrf(client)
    )
    assert short.status_code == 400
    assert short.json()["details"] == {"field": "new"}
    wrong = await client.post(
        "/api/auth/password", json={"current": "nope-nope-nope", "new": "brand-new-password"},
        headers=_csrf(client),
    )
    assert wrong.status_code == 403
    ok = await client.post(
        "/api/auth/password", json={"current": PASSWORD, "new": "brand-new-password"},
        headers=_csrf(client),
    )
    assert ok.status_code == 200
    client.cookies.clear()
    assert (await _login(client, admin_user.email)).status_code == 403
    assert (await _login(client, admin_user.email, "brand-new-password")).status_code == 200


async def test_validation_error_shape(admin_app):
    client, _ = admin_app
    r = await client.post(LOGIN_PATH, json={"email": "x@example.com", "password": 5})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "INVALID_ORDER" and "5" not in str(body["details"])


@pytest.mark.parametrize(("method", "path"), _protected_routes())
async def test_api_routes_require_login(admin_app, method, path):
    client, _ = admin_app
    r = await client.request(method, re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path))
    assert r.status_code == 401
    assert r.json() == {"code": "FORBIDDEN", "message": "login richiesto", "details": None}


def test_protected_routes_cover_auth_endpoints():
    paths = {p for _, p in _protected_routes()}
    assert {"/api/auth/me", "/api/auth/logout", "/api/auth/password"} <= paths


async def test_seed_admin_idempotent(db_session):
    from smtrading.admin.auth import verify_password
    from smtrading.admin.models import AdminUser
    from smtrading.seed import seed_admin

    await seed_admin(db_session, "Seed-Admin@Example.com", "seed-password-1")
    await seed_admin(db_session, "seed-admin@example.com", "another-password")
    rows = (
        await db_session.execute(
            select(AdminUser).where(AdminUser.email == "seed-admin@example.com")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert await verify_password(rows[0].password_hash, "seed-password-1")  # not overwritten


async def test_run_seed_creates_admin(db_session):
    from smtrading.admin.models import AdminUser
    from smtrading.seed import run_seed

    creds = ("settings-admin@example.com", "settings-password")
    await run_seed(db_session, admin=creds)
    await run_seed(db_session, admin=creds)
    emails = (await db_session.execute(select(AdminUser.email))).scalars().all()
    assert emails.count("settings-admin@example.com") == 1
