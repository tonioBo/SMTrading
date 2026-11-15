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


async def _rows(sm, **where) -> list[UserActivity]:
    async with sm() as s:
        q = select(UserActivity).order_by(UserActivity.id)
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
    """Enumerates ALL the app's unsafe routes: each request gives exactly one row with the
    route template (never the raw path), the path ids and the outcome."""
    email, user_id = await env.add_user("viewer")
    c = await env.client(email)
    before = len(await _rows(env.sm))
    calls = [(m, p) for m, p in _app_routes()
             if m in UNSAFE and p != "/api/auth/logout"] + [("POST", "/api/auth/logout")]
    sent = {}
    for method, path in calls:
        url = _fill(path)
        r = await c.request(method, url, json={})
        sent[(method, path)] = (url, r.status_code)
    rows = (await _rows(env.sm))[before:]
    assert len(rows) == len(calls)  # one row per request, no duplicate
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
    assert len(await _rows(env.sm, action="portfolios.create")) == 1
    r = await a.post("/api/portfolios", json={"name": "", "initial_cash": "x"})
    assert r.status_code == 400
    assert len(await _rows(env.sm, action="portfolios.create")) == 1


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
