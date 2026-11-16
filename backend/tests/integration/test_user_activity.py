"""User activity log («Registro attività»): every unsafe /api request of a person is
recorded (route coverage), with who/when/where, the outcome and a summary never holding
a secret; the access events; the same-transaction write and the failure fallback; the
append-only table; the admin-only API and its CSV."""
# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

import csv
import json
import re
import uuid

import pyotp
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_admin_users import (
    PASSWORD,
    UNSAFE,
    _app_routes,
    _audit,
    _fill,
    _outbox_texts,
    _telegram,
    env,  # noqa: F401  (the fixture)
)

from smtrading.useractivity import catalog
from smtrading.useractivity.models import UserActivity
from smtrading.useractivity.sanitize import SUMMARY_MAX_CHARS

TG_TOKEN = "123456789:AAH" + "s" * 32


async def _rows(sm, provisional: bool | None = False, **where) -> list[UserActivity]:
    """The final rows by default (`provisional=None`: every row)."""
    async with sm() as s:
        q = select(UserActivity).order_by(UserActivity.id)
        if provisional is not None:
            q = q.where(UserActivity.provisional.is_(provisional))
        for k, v in where.items():
            q = q.where(getattr(UserActivity, k) == v)
        return list(await s.scalars(q))


def _dump(rows: list[UserActivity]) -> str:
    return json.dumps([{c.name: str(getattr(r, c.name)) for c in UserActivity.__table__.columns}
                       for r in rows])


# -- coverage ---------------------------------------------------------------------------

def test_every_unsafe_route_has_an_action():
    """A new unsafe route without an entry in the catalog fails here."""
    missing = [(m, p) for m, p in _app_routes()
               if m in UNSAFE and (m, p) not in catalog.ROUTE_ACTIONS]
    assert missing == []
    for action in catalog.ROUTE_ACTIONS.values():
        assert catalog.label(action) != action, action
        assert catalog.category(action) in catalog.CATEGORIES, action


async def test_every_unsafe_route_records_one_activity_row(env):
    """Enumerates ALL the app's unsafe routes: each request gives exactly one final row with
    the route template (never the raw path), the path ids and the outcome (and, when it
    committed, one provisional row of its own commit with the same request_id)."""
    email, user_id = await env.add_user("viewer")
    c = await env.client(email)
    before = len(await _rows(env.sm))
    before_all = len(await _rows(env.sm, provisional=None))
    calls = [(m, p) for m, p in _app_routes()
             if m in UNSAFE and p != "/api/auth/logout"] + [("POST", "/api/auth/logout")]
    sent = {}
    for method, path in calls:
        url = _fill(path)
        r = await c.request(method, url, json={})
        sent[(method, path)] = (url, r.status_code)
    rows = (await _rows(env.sm))[before:]
    assert len(rows) == len(calls)  # one final row per request, no duplicate
    pending = [r for r in (await _rows(env.sm, provisional=None))[before_all:] if r.provisional]
    assert {r.request_id for r in pending} <= {r.request_id for r in rows}
    assert all(r.outcome == "pending" and r.status is None for r in pending)
    by_route = {(r.method, r.route): r for r in rows}
    assert set(by_route) == set(calls)
    for (method, path), (url, status) in sent.items():
        row = by_route[(method, path)]
        assert row.status == status, (method, path)
        assert row.outcome == ("ok" if status < 400 else
                               "denied" if status in (401, 403) else "error"), (method, path)
        assert row.action == catalog.action_for(method, path, row.outcome, row.error_code)
        for name in re.findall(r"\{([^}]+)\}", path):
            assert row.target[name] in url, (path, name)
        if path != "/api/auth/login":  # the login is not a session request
            assert (row.user_id, row.email, row.role) == (user_id, email, "viewer"), path
        assert row.ip == "127.0.0.1"


async def test_unknown_api_path_is_recorded_too(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    r = await a.post("/api/no-such-route/abc", json={})
    assert r.status_code == 404
    (row,) = await _rows(env.sm, method="POST", route="/api/no-such-route/abc")
    assert row.action == "other.unknown" and row.outcome == "error"


async def test_reads_and_non_api_paths_are_not_recorded(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    before = len(await _rows(env.sm))
    await a.get("/api/portfolios")
    await a.get("/health")
    assert len(await _rows(env.sm)) == before + 0  # the login row came before


# -- contents -----------------------------------------------------------------------------

async def test_row_contents_and_link_with_the_admin_audit(env):
    email, admin_id = await env.add_user("admin")
    a = await env.client(email)
    a.headers["User-Agent"] = "pytest-browser/1.0"
    r = await a.post("/api/users", json={"email": "new@example.com", "role": "viewer"})
    assert r.status_code == 200, r.text
    temp = r.json()["temp_password"]
    new_id = r.json()["user"]["id"]
    (row,) = await _rows(env.sm, action="users.create")
    assert (row.user_id, row.email, row.role) == (admin_id, email, "admin")
    assert (row.method, row.route, row.outcome, row.status) == ("POST", "/api/users", "ok", 200)
    assert row.ip == "127.0.0.1" and row.user_agent == "pytest-browser/1.0"
    assert row.summary == {"email": "new@example.com", "role": "viewer"}
    assert row.target == {} and row.ts is not None
    assert temp not in _dump(await _rows(env.sm))
    (audit,) = await _audit(env.sm, "admin:user.create")
    assert audit.params["request_id"] == str(row.request_id)

    r = await a.put(f"/api/users/{new_id}/role", json={"role": "bogus"})
    assert r.status_code == 400
    (bad,) = await _rows(env.sm, action="users.set_role")
    assert bad.target == {"user_id": new_id}
    assert (bad.outcome, bad.status, bad.error_code) == ("error", 400, "INVALID_ORDER")
    assert bad.error_message


async def test_no_secret_is_ever_recorded(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    secrets = ["Sup3rSecretPassw0rd", "AKXXXXXXXXXXXXXXXXXX", "sk-very-secret-value-123",
               TG_TOKEN, "654321"]
    await a.post("/api/auth/password", json={"current": secrets[0], "new": "x" * 12})
    await a.post("/api/live-links", json={"name": "L", "kind": "alpaca_live",
                                          "key_id": secrets[1], "secret_key": secrets[2]})
    await a.put("/api/telegram/config", json={"bot_token": secrets[3], "chat_id": "1"})
    await a.post("/api/auth/totp/enable", json={"code": secrets[4]})
    # a token under an innocent key is caught by the log secret filter
    await a.post("/api/portfolios", json={"name": f"note {TG_TOKEN}", "initial_cash": "1"})
    await a.patch("/api/connectors/zz-unknown",
                  json={"config": {"nested": {"api_key": secrets[2]}, "list": [secrets[1]]}})
    anon = await env.client(email)  # a fresh client, then a failed login
    await anon.post("/api/auth/login", json={"email": email, "password": secrets[0]})
    dump = _dump(await _rows(env.sm))
    for s in secrets:
        assert s not in dump, s
    (pw,) = await _rows(env.sm, action="auth.password_change")
    assert set(pw.summary) == {"current", "new"} and pw.summary["current"] == "••••"


async def test_summary_is_capped(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    await a.post("/api/portfolios", json={"name": "n" * 5000, "items": list(range(500)),
                                          "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}})
    (row,) = await _rows(env.sm, action="portfolios.create")
    assert len(json.dumps(row.summary)) <= SUMMARY_MAX_CHARS + 200


# -- access events --------------------------------------------------------------------------

async def test_access_events(env):
    email, user_id = await env.add_user("viewer")
    c = await env.client(email)
    (login,) = await _rows(env.sm, action="auth.login")
    assert (login.user_id, login.email, login.role, login.outcome) == (
        user_id, email, "viewer", "ok")
    assert login.summary["password"] == "••••"

    await env.client(email, "wrong-password-xx", expect=403)
    await env.client("nobody@example.com", "wrong-password-xx", expect=403)
    failed = await _rows(env.sm, action="auth.login_failed")
    assert [(f.email, f.user_id, f.outcome) for f in failed] == [
        (email, None, "denied"), ("nobody@example.com", None, "denied")]
    for _ in range(5):
        await env.client("locked@example.com", "wrong-password-xx", expect=403)
    await env.client("locked@example.com", "wrong-password-xx", expect=429)
    (locked,) = await _rows(env.sm, action="auth.login_locked")
    assert locked.email == "locked@example.com" and locked.error_code == "RATE_LIMITED"

    assert (await c.post("/api/auth/totp/setup")).status_code == 200
    assert (await env.user(user_id)).totp_secret_encrypted is not None
    from smtrading.admin.auth import _totp_secret

    code = pyotp.TOTP(_totp_secret(await env.user(user_id))).now()
    r = await c.post("/api/auth/totp/enable", json={"code": code})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
    plain = _totp_secret(await env.user(user_id))
    r = await c.post("/api/auth/totp/disable", json={"code": await env.code(plain)})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
    assert (await c.post("/api/auth/sessions/revoke-others")).status_code == 200
    r = await c.post("/api/auth/password", json={"current": PASSWORD, "new": "another-pass-1"})
    assert r.status_code == 200
    c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
    assert (await c.post("/api/auth/logout")).status_code == 204
    mine = [r.action for r in await _rows(env.sm, user_id=user_id)]
    assert mine == ["auth.login", "auth.totp_setup", "auth.totp_enable", "auth.totp_disable",
                    "auth.sessions_revoke", "auth.password_change", "auth.logout"]
    for r in await _rows(env.sm):
        assert code not in json.dumps(r.summary)


async def test_forced_password_change_event(env):
    email, user_id = await env.add_user("viewer", must_change=True)
    c = await env.client(email)
    r = await c.post("/api/auth/password", json={"current": PASSWORD, "new": "my-own-pass-1"})
    assert r.status_code == 200
    (row,) = await _rows(env.sm, action="auth.password_forced_change")
    assert row.user_id == user_id and row.outcome == "ok"
    denied = await c.get("/api/portfolios")  # reads are never recorded
    assert denied.status_code in (200, 403)


async def test_step_up_totp_lockout_event(env):
    secret = pyotp.random_base32()
    email, admin_id = await env.add_user("admin", totp_secret=secret)
    a = await env.client(email, totp=await env.code(secret))
    for _ in range(5):
        r = await a.post("/api/users", json={"email": "x@example.com", "role": "admin",
                                             "totp": "000000"})
        assert r.status_code == 403
    r = await a.post("/api/users", json={"email": "x@example.com", "role": "admin",
                                         "totp": "000000"})
    assert r.status_code == 429
    (lock,) = await _rows(env.sm, action="auth.totp_lockout")
    assert lock.user_id == admin_id and lock.route == "/api/users"
    creates = await _rows(env.sm, action="users.create")
    assert len(creates) == 6 and creates[-1].error_code == "RATE_LIMITED"
    assert lock.request_id == creates[4].request_id


# -- transaction and failures -----------------------------------------------------------------

async def test_committed_change_is_recorded_in_the_same_transaction(env, monkeypatch):
    """The after-response writer is broken: a committing route still has its row (written
    by its own commit), a route that does not commit has none."""
    from smtrading.useractivity import recorder

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    email, _ = await env.add_user("admin")
    a = await env.client(email)
    monkeypatch.setattr(recorder, "_insert_rows", broken)
    r = await a.post("/api/portfolios", json={"name": "Tx", "initial_cash": "100"})
    assert r.status_code == 200, r.text
    (row,) = await _rows(env.sm, provisional=None, action="portfolios.create")
    assert (row.provisional, row.outcome) == (True, "pending")
    r = await a.post("/api/portfolios", json={"name": "", "initial_cash": "x"})
    assert r.status_code == 400
    assert len(await _rows(env.sm, provisional=None, action="portfolios.create")) == 1


async def test_logging_failure_never_breaks_the_action(env, monkeypatch):
    from smtrading.useractivity import recorder

    await _telegram(env.sm)
    email, _ = await env.add_user("admin")
    a = await env.client(email)

    def broken(*args, **kwargs):
        raise RuntimeError("activity row cannot be built")

    monkeypatch.setattr(recorder.ActivityContext, "build_row", broken)
    for name in ("Uno", "Due"):
        r = await a.post("/api/portfolios", json={"name": name, "initial_cash": "100"})
        assert r.status_code == 200, r.text
    names = [p["name"] for p in (await a.get("/api/portfolios")).json()]
    assert {"Uno", "Due"} <= set(names)
    alerts = [t for t in await _outbox_texts(env.sm) if "Registro attività" in t]
    assert len(alerts) == 1  # once, not per request


# -- append-only ---------------------------------------------------------------------------

async def test_user_activity_is_append_only(env):
    email, _ = await env.add_user("admin")
    await env.client(email)
    (row,) = await _rows(env.sm, action="auth.login")
    for stmt in ("update user_activity set email = 'x' where id = :i",
                 "delete from user_activity where id = :i",
                 "truncate user_activity"):
        async with env.sm() as s:
            with pytest.raises(DBAPIError, match="USER_ACTIVITY_APPEND_ONLY"):
                await s.execute(text(stmt), {"i": row.id})


# -- the API ---------------------------------------------------------------------------------

async def test_activity_api_filters_paging_and_viewer_refused(env):
    email, _ = await env.add_user("admin")
    viewer, _ = await env.add_user("viewer")
    a = await env.client(email)
    v = await env.client(viewer)
    await a.post("/api/portfolios", json={"name": "P1", "initial_cash": "100"})
    await a.post("/api/portfolios", json={"name": "", "initial_cash": "x"})
    await v.post("/api/portfolios", json={"name": "P2", "initial_cash": "100"})
    assert (await v.get("/api/activity-log")).status_code == 403
    assert (await v.get("/api/activity-log.csv")).status_code == 403
    assert (await v.get("/api/activity-log/catalog")).status_code == 403

    rows = (await a.get("/api/activity-log")).json()
    assert [r["action"] for r in rows][:3] == ["portfolios.create"] * 3
    top = rows[0]
    assert top["email"] == viewer and top["outcome"] == "denied"
    assert top["action_label"] == catalog.label("portfolios.create")
    assert top["category"] == "portfolios" and top["category_label"] == "Portafogli"
    mine = (await a.get("/api/activity-log", params={"email": email})).json()
    assert {r["email"] for r in mine} == {email}
    errs = (await a.get("/api/activity-log", params={"outcome": "error"})).json()
    assert [r["status"] for r in errs] == [400]
    auth = (await a.get("/api/activity-log", params={"category": "auth"})).json()
    assert {r["action"] for r in auth} == {"auth.login"}
    page = (await a.get("/api/activity-log", params={"limit": 2})).json()
    older = (await a.get("/api/activity-log",
                         params={"limit": 2, "before_id": page[-1]["id"]})).json()
    assert older[0]["id"] < page[-1]["id"]
    future = (await a.get("/api/activity-log",
                          params={"from": "2999-01-01T00:00:00+00:00"})).json()
    assert future == []
    past = (await a.get("/api/activity-log", params={"to": "2000-01-01T00:00:00+00:00"})).json()
    assert past == []
    assert (await a.get("/api/activity-log", params={"outcome": "bogus"})).status_code == 400
    cat = (await a.get("/api/activity-log/catalog")).json()
    assert {"key": "auth", "label": "Accesso"} in cat["categories"]
    assert {"key": "users.create", "label": catalog.label("users.create"),
            "category": "users"} in cat["actions"]


async def test_activity_csv(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    a.headers["User-Agent"] = "=HYPERLINK(\"http://x\")"
    await a.post("/api/portfolios", json={"name": "=1+1", "initial_cash": "100"})
    r = await a.get("/api/activity-log.csv", params={"category": "portfolios"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "registro-attivita" in r.headers["content-disposition"]
    raw = r.content
    assert raw.startswith("﻿".encode())
    body = raw.decode("utf-8-sig")
    lines = body.strip().split("\r\n")
    assert lines[0].split(";")[:4] == ["id", "data", "utente", "ruolo"]
    assert len(lines) == 2
    assert catalog.label("portfolios.create") in lines[1]
    (cells,) = list(csv.reader(lines[1:], delimiter=";"))
    assert "'=HYPERLINK(\"http://x\")" in cells
    assert not [c for c in cells if c[:1] in ("=", "+", "@")]


async def test_request_ids_are_unique_per_request(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    for _ in range(3):
        await a.post("/api/portfolios", json={"name": f"R{uuid.uuid4().hex[:4]}",
                                              "initial_cash": "1"})
    rows = await _rows(env.sm, action="portfolios.create")
    assert len({r.request_id for r in rows}) == 3


# -- review fixes: flush errors, provisional rows, anonymous rate limit --------------------

async def test_route_flush_error_propagates_unchanged(env):
    """The route's own flush error at commit is the error the route sees (the activity
    hook never swallows it into a broken session)."""
    from sqlalchemy.exc import IntegrityError

    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.useractivity.context import ActivityContext, _current
    from smtrading.useractivity.recorder import bind_session

    email, _ = await env.add_user("admin")
    ctx = ActivityContext({"type": "http", "method": "POST", "path": "/api/x", "headers": []})
    token = _current.set(ctx)
    try:
        async with env.sm() as s:
            bind_session(s)
            s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
            with pytest.raises(IntegrityError):
                await s.commit()
    finally:
        _current.reset(token)
    assert ctx.pending is False and ctx.committed is False


async def test_commit_early_route_that_fails_later(env, monkeypatch):
    """Disarm commits before its slow work: if that work fails, the commit's row stays
    «pending» and the final row (same request_id) says error; the API and the CSV show
    the final outcome only."""
    from smtrading.admin import routes_live
    from smtrading.common.errors import ErrorCode, SMTError

    async def found(session, link_id):
        await session.execute(text("select 1"))  # the request transaction begins

    async def fails(*args, **kwargs):
        raise SMTError(ErrorCode.INVALID_ORDER, "disattivazione non riuscita")

    monkeypatch.setattr(routes_live, "_live_link", found)
    monkeypatch.setattr(routes_live, "disarm_live_link_now", fails)
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    r = await a.post(f"/api/live-links/{uuid.uuid4()}/disarm")
    assert r.status_code == 400
    rows = await _rows(env.sm, provisional=None, action="emergency.live_disarm")
    assert [(x.provisional, x.outcome, x.status) for x in rows] == [
        (True, "pending", None), (False, "error", 400)]
    assert rows[0].request_id == rows[1].request_id
    listed = (await a.get("/api/activity-log",
                          params={"category": "emergency"})).json()
    assert [(x["outcome"], x["request_id"]) for x in listed] == [
        ("error", str(rows[1].request_id))]
    assert listed[0]["provisional_rows"] == 1
    csv_lines = (await a.get("/api/activity-log.csv", params={"category": "emergency"})
                 ).content.decode("utf-8-sig").strip().split("\r\n")
    assert len(csv_lines) == 2 and str(rows[1].request_id) in csv_lines[1]


async def test_a_pending_row_without_its_final_row_is_shown_pending(env, monkeypatch):
    from smtrading.useractivity import recorder

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    email, _ = await env.add_user("admin")
    a = await env.client(email)
    monkeypatch.setattr(recorder, "_insert_rows", broken)
    assert (await a.post("/api/portfolios", json={"name": "P", "initial_cash": "1"})
            ).status_code == 200
    monkeypatch.undo()
    listed = (await a.get("/api/activity-log", params={"category": "portfolios"})).json()
    assert [x["outcome"] for x in listed] == ["pending"]


async def test_anonymous_failures_are_rate_limited_and_coalesced(env, monkeypatch):
    import time
    from types import SimpleNamespace

    import httpx
    from redis.asyncio import Redis

    from smtrading.useractivity import throttle

    # the windows are fixed minutes: the throttle's clock is frozen mid-minute, so the
    # requests never straddle two windows (the test only; the app keeps the real clock)
    frozen = (time.time() // throttle.WINDOW_S) * throttle.WINDOW_S + throttle.WINDOW_S / 2
    monkeypatch.setattr(throttle, "time", SimpleNamespace(time=lambda: frozen))
    anon = httpx.AsyncClient(transport=env.transport, base_url="http://test",
                             headers={"User-Agent": "bot/" + "x" * 300})
    env.clients.append(anon)
    n = throttle.ANON_LIMIT + 5
    for _ in range(n):
        r = await anon.post("/api/portfolios", json={"name": "spam", "password": "x"})
        assert r.status_code == 401
    rows = await _rows(env.sm, action="portfolios.create")
    assert len(rows) == throttle.ANON_LIMIT
    assert all(x.user_id is None and x.summary == {} for x in rows)
    assert all(len(x.user_agent) <= 120 for x in rows)
    redis = Redis.from_url(env.redis_url)
    try:
        assert await throttle.flush_suppressed(env.sm, redis) == 0  # the window is open
        assert await throttle.flush_suppressed(env.sm, redis, now=frozen + 120) == 1
        assert await throttle.flush_suppressed(env.sm, redis, now=frozen + 120) == 0
    finally:
        await redis.aclose()
    rows = await _rows(env.sm, action="portfolios.create")
    assert len(rows) == throttle.ANON_LIMIT + 1
    assert rows[-1].summary == {"_soppressi": 5} and rows[-1].ip == "127.0.0.1"
    assert rows[-1].outcome == "denied" and rows[-1].route == "/api/portfolios"
    # unknown routes too, only within the rate
    for _ in range(n):
        await anon.post("/api/nope", json={})
    assert len(await _rows(env.sm, action="other.unknown")) == throttle.ANON_LIMIT


async def test_authenticated_failures_are_never_rate_limited(env):
    from smtrading.useractivity import throttle

    email, _ = await env.add_user("viewer")
    c = await env.client(email)
    n = throttle.ANON_LIMIT + 5
    for _ in range(n):
        await c.post("/api/portfolios", json={"name": "x", "initial_cash": "1"})
    assert len(await _rows(env.sm, action="portfolios.create")) == n


async def test_attempted_login_email_must_look_like_an_email(env):
    c = await env.client("not-an-email", "wrong-password-xx", expect=403)
    assert c is not None
    (row,) = await _rows(env.sm, action="auth.login_failed")
    assert row.email == "(non valido)"


async def test_email_free_text_and_anonymous_filters(env):
    email, _ = await env.add_user("admin")
    a = await env.client(email)
    await env.client("ghost@example.com", "wrong-password-xx", expect=403)
    part = email.split("@")[0].upper()
    found = (await a.get("/api/activity-log", params={"email": part})).json()
    assert found and {x["email"] for x in found} == {email}
    anon = (await a.get("/api/activity-log", params={"anonymous": "true"})).json()
    assert [x["email"] for x in anon] == ["ghost@example.com"]
    assert all(x["user_id"] is None for x in anon)
