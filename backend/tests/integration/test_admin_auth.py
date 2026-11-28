import asyncio
import re
import time
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
PUBLIC_API_PATHS = {LOGIN_PATH, "/api/auth/logout"}  # logout is best effort
# the TV screens authenticate with the display link cookie, not a portal login (tested in test_admin_tv)
DISPLAY_LINK_PREFIX = "/api/tv-display"
NON_API_ROUTE_ALLOWLIST: set[str] = set()  # non-APIRoute routes allowed under /api/
PASSWORD = "correct-horse-battery"


def _api_route_contexts():
    from smtrading.api.main import create_app

    # iter_route_contexts flattens included routers (paths carry the router prefix)
    return [
        ctx for ctx in iter_route_contexts(create_app().routes)
        if (ctx.path or "").startswith("/api/")
    ]


def _protected_routes() -> list[tuple[str, str]]:
    return sorted(
        (method, ctx.path)
        for ctx in _api_route_contexts()
        if isinstance(ctx.original_route, APIRoute) and ctx.path not in PUBLIC_API_PATHS
        and not ctx.path.startswith(DISPLAY_LINK_PREFIX)
        for method in ctx.methods
    )


class AdminApp:
    """Unpacks as (client, sessionmaker); `new_client()` opens another client (own cookies)."""

    def __init__(self, client, sessionmaker, transport, redis_url):
        self.client, self.sessionmaker, self.transport = client, sessionmaker, transport
        self.redis_url = redis_url

    def __iter__(self):
        return iter((self.client, self.sessionmaker))

    def new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, base_url="http://test")


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
            yield AdminApp(
                client, async_sessionmaker(engine, expire_on_commit=False), transport, redis_url
            )
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


def _wrong_code(totp) -> str:
    valid = {totp.at(time.time() + d) for d in (-60, -30, 0, 30, 60)}
    return next(c for c in ("000000", "111111", "222222", "333333", "444444", "555555")
                if c not in valid)


async def _forget_used_codes(redis_url: str, admin_id) -> None:
    """Lift the replay guard so the test can use the current code again."""
    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url)
    try:
        keys = [k async for k in redis.scan_iter(f"adm:totp_used:{admin_id}:*")]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


async def test_login_then_me(admin_app, admin_user):
    client, sm = admin_app
    r = await _login(client, admin_user.email.upper())  # email is case-insensitive
    assert r.status_code == 200
    assert r.json() == {"email": admin_user.email, "totp_enabled": False, "role": "admin",
                        "must_change_password": False}
    set_cookie = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookie if c.startswith("smt_session="))
    csrf_cookie = next(c for c in set_cookie if c.startswith("smt_csrf="))
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "HttpOnly" not in csrf_cookie

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == r.json()

    from smtrading.admin.models import AdminUser

    async with sm() as s:
        user = await s.get(AdminUser, admin_user.id)
        assert user.last_login_at is not None


async def test_session_cookie_persists_for_the_absolute_lifetime(admin_app, admin_user):
    """The session survives a browser restart: the cookie carries Max-Age = 31 days."""
    client, _ = admin_app
    r = await _login(client, admin_user.email)
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    sess = next(c for c in cookies if c.startswith("smt_session="))
    assert f"Max-Age={31 * 24 * 3600}" in sess


async def test_session_ttl_is_sliding(admin_app, admin_user, redis_url):
    from redis.asyncio import Redis

    client, _ = admin_app
    await _login(client, admin_user.email)
    key = f"adm:sess:{client.cookies['smt_session']}"
    redis = Redis.from_url(redis_url)
    try:
        assert 12 * 3600 < await redis.ttl(key) <= 24 * 3600  # idle: 24 h
        await redis.expire(key, 60)
        assert (await client.get("/api/auth/me")).status_code == 200
        assert await redis.ttl(key) > 24 * 3600 - 60
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

    def forget():
        return _forget_used_codes(admin_app.redis_url, admin_user.id)

    await _login(client, admin_user.email)
    setup = await client.post("/api/auth/totp/setup", headers=_csrf(client))
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    assert "issuer=SMTrading" in setup.json()["otpauth_uri"]
    totp = pyotp.TOTP(secret)
    assert (await client.get("/api/auth/me")).json()["totp_enabled"] is False  # not active yet

    bad = await client.post(
        "/api/auth/totp/enable", json={"code": _wrong_code(totp)}, headers=_csrf(client)
    )
    assert bad.status_code == 403
    ok = await client.post("/api/auth/totp/enable", json={"code": totp.now()}, headers=_csrf(client))
    assert ok.status_code == 200 and ok.json()["totp_enabled"] is True

    await client.post("/api/auth/logout", headers=_csrf(client))
    client.cookies.clear()
    await forget()
    missing = await _login(client, admin_user.email)
    assert missing.status_code == 403
    assert missing.json()["details"] == {"totp_required": True}
    wrong = await _login(client, admin_user.email, totp=_wrong_code(totp))
    assert wrong.status_code == 403 and wrong.json()["message"] == "codice TOTP non valido"
    good = await _login(client, admin_user.email, totp=totp.now())
    assert good.status_code == 200 and good.json()["totp_enabled"] is True

    await forget()
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
    assert r.status_code == 204
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


async def test_logout_is_best_effort(admin_app, admin_user):
    client, _ = admin_app
    anon = await client.post("/api/auth/logout")  # no session, no CSRF
    assert anon.status_code == 204
    await _login(client, admin_user.email)
    sid = client.cookies["smt_session"]
    r = await client.post("/api/auth/logout")  # session but no CSRF header
    assert r.status_code == 204
    cleared = r.headers.get_list("set-cookie")
    assert any(c.startswith("smt_session=") and "Max-Age=0" in c for c in cleared)
    assert any(c.startswith("smt_csrf=") and "Max-Age=0" in c for c in cleared)
    client.cookies.clear()
    client.cookies.set("smt_session", sid)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_password_change_revokes_other_sessions(admin_app, admin_user):
    changer, _ = admin_app
    await _login(changer, admin_user.email)
    async with admin_app.new_client() as other:
        await _login(other, admin_user.email)
        assert (await other.get("/api/auth/me")).status_code == 200
        old_sid = changer.cookies["smt_session"]
        r = await changer.post(
            "/api/auth/password", json={"current": PASSWORD, "new": "brand-new-password"},
            headers=_csrf(changer),
        )
        assert r.status_code == 200
        assert changer.cookies["smt_session"] != old_sid  # re-issued
        assert (await changer.get("/api/auth/me")).status_code == 200
        assert (await changer.post("/api/auth/totp/setup", headers=_csrf(changer))).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401
    async with admin_app.new_client() as replay:
        replay.cookies.set("smt_session", old_sid)
        assert (await replay.get("/api/auth/me")).status_code == 401


async def test_totp_disable_revokes_other_sessions(admin_app, admin_user):
    changer, _ = admin_app
    await _login(changer, admin_user.email)
    secret = (await changer.post("/api/auth/totp/setup", headers=_csrf(changer))).json()["secret"]
    totp = pyotp.TOTP(secret)
    r = await changer.post("/api/auth/totp/enable", json={"code": totp.now()}, headers=_csrf(changer))
    assert r.status_code == 200
    assert (await changer.get("/api/auth/me")).json()["totp_enabled"] is True  # re-issued
    await _forget_used_codes(admin_app.redis_url, admin_user.id)
    async with admin_app.new_client() as other:
        assert (await _login(other, admin_user.email, totp=totp.now())).status_code == 200
        await _forget_used_codes(admin_app.redis_url, admin_user.id)
        off = await changer.post(
            "/api/auth/totp/disable", json={"code": totp.now()}, headers=_csrf(changer)
        )
        assert off.status_code == 200
        assert (await changer.get("/api/auth/me")).json()["totp_enabled"] is False
        assert (await other.get("/api/auth/me")).status_code == 401


async def test_totp_re_enable_does_not_revive_old_sessions(admin_app, admin_user):
    """A session revoked by TOTP disable must stay dead when TOTP is enabled again
    (same password, totp_enabled back to True): revocation is immediate, not only a
    fingerprint mismatch."""
    changer, _ = admin_app
    await _login(changer, admin_user.email)
    secret = (await changer.post("/api/auth/totp/setup", headers=_csrf(changer))).json()["secret"]
    totp = pyotp.TOTP(secret)
    assert (await changer.post(
        "/api/auth/totp/enable", json={"code": totp.now()}, headers=_csrf(changer)
    )).status_code == 200
    await _forget_used_codes(admin_app.redis_url, admin_user.id)
    async with admin_app.new_client() as other:
        assert (await _login(other, admin_user.email, totp=totp.now())).status_code == 200
        await _forget_used_codes(admin_app.redis_url, admin_user.id)
        assert (await changer.post(
            "/api/auth/totp/disable", json={"code": totp.now()}, headers=_csrf(changer)
        )).status_code == 200
        secret2 = (await changer.post(
            "/api/auth/totp/setup", headers=_csrf(changer))).json()["secret"]
        totp2 = pyotp.TOTP(secret2)
        assert (await changer.post(
            "/api/auth/totp/enable", json={"code": totp2.now()}, headers=_csrf(changer)
        )).status_code == 200
        assert (await changer.get("/api/auth/me")).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401


async def test_totp_code_cannot_be_replayed(admin_app, admin_user):
    client, _ = admin_app
    await _login(client, admin_user.email)
    secret = (await client.post("/api/auth/totp/setup", headers=_csrf(client))).json()["secret"]
    totp = pyotp.TOTP(secret)
    code = totp.now()
    assert (await client.post(
        "/api/auth/totp/enable", json={"code": code}, headers=_csrf(client)
    )).status_code == 200
    client.cookies.clear()
    reused = await _login(client, admin_user.email, totp=code)
    assert reused.status_code == 403 and reused.json()["message"] == "codice TOTP già usato"


async def test_oversized_fields_rejected(admin_app):
    client, _ = admin_app
    r = await _login(client, "x@example.com", "p" * 257)
    assert r.status_code == 400 and r.json()["details"] == {"fields": ["password"]}


def test_no_unexpected_non_api_routes_under_api():
    others = {
        ctx.path for ctx in _api_route_contexts() if not isinstance(ctx.original_route, APIRoute)
    }
    assert others <= NON_API_ROUTE_ALLOWLIST


def test_protected_routes_cover_auth_endpoints():
    paths = {p for _, p in _protected_routes()}
    assert {"/api/auth/me", "/api/auth/password", "/api/auth/totp/disable"} <= paths


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


@pytest.mark.parametrize(
    ("email", "password"), [("", "long-enough-password"), ("short@example.com", "short")]
)
async def test_seed_admin_refuses_bad_credentials(db_session, caplog, email, password):
    from smtrading.admin.models import AdminUser
    from smtrading.seed import seed_admin

    before = len((await db_session.execute(select(AdminUser.id))).all())
    with caplog.at_level("ERROR", logger="smtrading.seed"):
        await seed_admin(db_session, email, password)
    assert "admin not created" in caplog.text
    assert len((await db_session.execute(select(AdminUser.id))).all()) == before


async def test_seed_admin_existing_admin_is_not_an_error(db_session, caplog):
    from smtrading.seed import seed_admin

    await seed_admin(db_session, "kept@example.com", "long-enough-password")
    with caplog.at_level("ERROR", logger="smtrading.seed"):
        await seed_admin(db_session, "kept@example.com", "short")  # e.g. .env edited later
    assert "admin not created" not in caplog.text


async def _session_data(redis_url: str, sid: str) -> tuple[object, dict]:
    import json

    from redis.asyncio import Redis

    redis = Redis.from_url(redis_url)
    return redis, json.loads(await redis.get(f"adm:sess:{sid}"))


async def test_session_absolute_lifetime(admin_app, admin_user, redis_url):
    import json
    from datetime import UTC, datetime, timedelta

    client, _ = admin_app
    await _login(client, admin_user.email)
    sid = client.cookies["smt_session"]
    key = f"adm:sess:{sid}"
    redis, data = await _session_data(redis_url, sid)
    try:
        # 31 days minus 1 h old: still valid, but the idle renewal never goes past the cap
        data["created_at"] = (datetime.now(UTC) - timedelta(days=31, hours=-1)).isoformat()
        await redis.set(key, json.dumps(data), keepttl=True)
        assert (await client.get("/api/auth/me")).status_code == 200
        assert 0 < await redis.ttl(key) <= 3600
        # past 31 days: refused and destroyed, however recent the activity
        data["created_at"] = (datetime.now(UTC) - timedelta(days=31, seconds=1)).isoformat()
        await redis.set(key, json.dumps(data), ex=12 * 3600)
        assert (await client.get("/api/auth/me")).status_code == 401
        assert await redis.exists(key) == 0
        assert not await redis.sismember(f"adm:sess_idx:{admin_user.id}", sid)
    finally:
        await redis.aclose()


async def test_login_failures_counted_per_entry(admin_app, admin_user):
    client, _ = admin_app
    lan = {"X-SMT-Entry": "lan"}
    for i in range(5):  # a missing header counts as the LAN entry
        r = await client.post(LOGIN_PATH, json={
            "email": admin_user.email, "password": "wrong-password",
        }, headers=lan if i % 2 else {})
        assert r.status_code == 403
    limited = await client.post(
        LOGIN_PATH, json={"email": admin_user.email, "password": PASSWORD}, headers=lan
    )
    assert limited.status_code == 429
    async with admin_app.new_client() as local:
        r = await local.post(
            LOGIN_PATH, json={"email": admin_user.email, "password": PASSWORD},
            headers={"X-SMT-Entry": "local"},
        )
        assert r.status_code == 200  # the LAN failures do not lock the local entry out
    bogus = await client.post(
        LOGIN_PATH, json={"email": admin_user.email, "password": PASSWORD},
        headers={"X-SMT-Entry": "anything"},
    )
    assert bogus.status_code == 429  # an unknown entry is the LAN one


async def test_revoke_other_sessions(admin_app, admin_user, redis_url):
    from redis.asyncio import Redis

    me, _ = admin_app
    await _login(me, admin_user.email)
    async with admin_app.new_client() as other, admin_app.new_client() as third:
        await _login(other, admin_user.email)
        await _login(third, admin_user.email)
        r = await me.post("/api/auth/sessions/revoke-others", headers=_csrf(me))
        assert r.status_code == 200
        assert r.json() == {"revoked": 2}
        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await third.get("/api/auth/me")).status_code == 401
        assert (await me.get("/api/auth/me")).status_code == 200
        r = await me.post("/api/auth/sessions/revoke-others", headers=_csrf(me))
        assert r.json() == {"revoked": 0}
    assert (await me.post("/api/auth/sessions/revoke-others")).status_code == 403  # CSRF
    redis = Redis.from_url(redis_url)
    try:
        members = await redis.smembers(f"adm:sess_idx:{admin_user.id}")
        assert {m.decode() for m in members} == {me.cookies["smt_session"]}
        await me.post("/api/auth/logout")
        assert await redis.scard(f"adm:sess_idx:{admin_user.id}") == 0
    finally:
        await redis.aclose()


@pytest.mark.parametrize(("scheme", "secure"), [("https", True), ("http", False)])
async def test_cookies_secure_only_over_https(admin_app, admin_user, scheme, secure):
    async with httpx.AsyncClient(
        transport=admin_app.transport, base_url=f"{scheme}://test"
    ) as client:
        r = await _login(client, admin_user.email)
        assert r.status_code == 200
        cookies = r.headers.get_list("set-cookie")
        assert len(cookies) == 2
        assert all(("; Secure" in c) is secure for c in cookies)
