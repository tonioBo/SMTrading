"""Admin API: connectors, provider priorities, markets/instruments, system and backup."""

import asyncio
import json
import uuid
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal


PASSWORD = "correct-horse-battery"


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    """A logged-in client (CSRF header set) on a seeded fresh database.

    Yields a namespace with `client`, `sm` (sessionmaker), `qs` (the FakeMarketData of
    the app runtime) and `redis_url`."""
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
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("200.00"), D("200.10"), D("200.05"))
    qs.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
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
            ns.client, ns.sm, ns.qs, ns.redis_url = client, sm, qs, redis_url
            yield ns
    finally:
        stop.set()
        await lifespan
        await engine.dispose()


class _FakeProvider:
    name = "fake"
    requires_key = True
    capabilities = frozenset()
    asset_classes = frozenset()
    seen_config: ClassVar[dict] = {}
    fail = False

    def __init__(self, config, http):
        type(self).seen_config = config

    async def health_check(self):
        if type(self).fail:
            raise RuntimeError("boom token=" + str(type(self).seen_config.get("api_key")))


async def _add_fake_connector(admin, monkeypatch, name="fake"):
    from smtrading.connectors.models import Connector
    from smtrading.marketdata.registry import PROVIDERS

    monkeypatch.setitem(PROVIDERS, name, _FakeProvider)
    async with admin.sm() as s:
        s.add(Connector(name=name, enabled=False))
        await s.commit()


async def test_connectors_masked_and_patch_keeps_masked(admin, monkeypatch):
    await _add_fake_connector(admin, monkeypatch)
    c = admin.client
    r = await c.patch("/api/connectors/fake", json={
        "enabled": True, "rate_limit_per_min": 30,
        "config": {"api_key": "abcdef123456", "api_secret": "xy", "region": "eu"},
    })
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["config_masked"] == {"api_key": "••••3456", "api_secret": "••••", "region": "eu"}
    assert row["enabled"] is True and row["rate_limit_per_min"] == 30
    assert row["implemented"] is True and row["requires_key"] is True
    assert row["secret_keys"] == ["api_key", "api_secret"]  # the web app's secret fields

    # the masked value sent back keeps the stored secret; other keys change
    r = await c.patch("/api/connectors/fake", json={
        "config": {"api_key": "••••3456", "api_secret": "newsecret99", "region": "us"},
    })
    assert r.status_code == 200
    assert r.json()["config_masked"]["api_secret"] == "••••et99"
    async with admin.sm() as s:
        from smtrading.connectors.service import get_connector_config

        stored = await get_connector_config(s, "fake")
    assert stored == {"api_key": "abcdef123456", "api_secret": "newsecret99", "region": "us"}

    listed = {x["name"]: x for x in (await c.get("/api/connectors")).json()}
    assert "abcdef" not in json.dumps(listed)
    assert listed["yahoo"]["implemented"] is True
    assert listed["revolutx"]["implemented"] is True  # registered since plan 6
    assert (await c.patch("/api/connectors/nope", json={"enabled": True})).status_code == 404
    assert (await c.patch("/api/connectors/fake", json={"rate_limit_per_min": 0})).status_code == 400


async def test_connector_test_records_health(admin, monkeypatch):
    await _add_fake_connector(admin, monkeypatch)
    c = admin.client
    await c.patch("/api/connectors/fake", json={"config": {"api_key": "k-123456"}})
    _FakeProvider.fail = False
    r = await c.post("/api/connectors/fake/test")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["error"] is None
    assert _FakeProvider.seen_config == {"api_key": "k-123456"}  # decrypted, not masked
    row = next(x for x in (await c.get("/api/connectors")).json() if x["name"] == "fake")
    assert row["health_status"] == "ok" and row["last_checked_at"]

    _FakeProvider.fail = True
    try:
        r = await c.post("/api/connectors/fake/test")
    finally:
        _FakeProvider.fail = False
    assert r.json()["ok"] is False and "boom" in r.json()["error"]
    assert "k-123456" not in r.json()["error"] and "••••" in r.json()["error"]
    row = next(x for x in (await c.get("/api/connectors")).json() if x["name"] == "fake")
    assert row["health_status"] == "error" and "boom" in row["last_error"]
    assert "k-123456" not in row["last_error"]
    assert (await c.post("/api/connectors/nope/test")).status_code == 404


async def test_provider_priorities(admin):
    c = admin.client
    matrix = (await c.get("/api/provider-priorities")).json()
    row = next(x for x in matrix if (x["asset_class"], x["capability"]) == ("crypto", "quote"))
    assert row["providers"][0] == "kraken"
    body = {"asset_class": "crypto", "capability": "quote", "providers": ["yahoo", "kraken"]}
    r = await c.put("/api/provider-priorities", json=body)
    assert r.status_code == 200 and r.json()["providers"] == ["yahoo", "kraken"]
    matrix = (await c.get("/api/provider-priorities")).json()
    row = next(x for x in matrix if (x["asset_class"], x["capability"]) == ("crypto", "quote"))
    assert row["providers"] == ["yahoo", "kraken"]

    r = await c.put("/api/provider-priorities", json={**body, "providers": ["yahoo", "ghost"]})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    for bad in ({**body, "asset_class": "bonds"}, {**body, "capability": "magic"}):
        assert (await c.put("/api/provider-priorities", json=bad)).status_code == 400


_TRACK_KEYS = ["md:tracked", *(f"md:tracked:{s}" for s in (
    "orders", "positions", "alerts", "watchlists", "admin"))]


async def test_track_untrack_and_stream_status(admin):
    import redis.asyncio as aioredis

    c = admin.client
    redis = aioredis.Redis.from_url(admin.redis_url)
    try:
        await redis.delete(*_TRACK_KEYS, "md:stream_status")
        st = (await c.get("/api/marketdata/stream-status")).json()
        assert st["available"] is False and st["streams"] == {}
        await redis.set("md:stream_status", json.dumps(
            {"streams": {"kraken:crypto": {"symbols": ["BTC-USD"], "state": "connected"}},
             "polling": []}), ex=30)
        st = (await c.get("/api/marketdata/stream-status")).json()
        assert st["available"] is True
        assert st["streams"]["kraken:crypto"]["state"] == "connected"

        r = await c.post("/api/marketdata/track", json={"symbols": ["aapl", "BTC-USD"]})
        assert r.status_code == 200, r.text
        assert {m.decode() for m in await redis.smembers("md:tracked:admin")} == {"AAPL", "BTC-USD"}
        assert (await c.get("/api/marketdata/stream-status")).json()["tracked"] == [
            "AAPL", "BTC-USD"]
        r = await c.post("/api/marketdata/untrack", json={"symbols": ["AAPL"]})
        assert r.status_code == 200
        assert r.json() == {"untracked": ["AAPL"], "still_tracked": {}}
        assert {m.decode() for m in await redis.smembers("md:tracked:admin")} == {"BTC-USD"}

        # an agent's watchlist keeps following: the admin's untrack reports it
        await redis.sadd("md:tracked:watchlists", "BTC-USD")
        await redis.sadd("md:tracked", "BTC-USD")
        st = (await c.get("/api/marketdata/stream-status")).json()
        assert st["tracked_sources"] == {"BTC-USD": ["admin", "watchlists"]}
        r = await c.post("/api/marketdata/untrack", json={"symbols": ["BTC-USD"]})
        assert r.json() == {"untracked": ["BTC-USD"], "still_tracked": {"BTC-USD": ["watchlists"]}}
        st = (await c.get("/api/marketdata/stream-status")).json()
        assert st["tracked"] == ["BTC-USD"] and st["tracked_sources"] == {"BTC-USD": ["watchlists"]}

        r = await c.post("/api/marketdata/track", json={"symbols": ["NOPE-XX"]})
        assert r.status_code == 400 and r.json()["code"] == "SYMBOL_NOT_FOUND"
        assert (await c.post("/api/marketdata/track", json={"symbols": []})).status_code == 400
        assert (await c.post("/api/marketdata/track", json={"symbols": ["a b"]})).status_code == 400
    finally:
        await redis.delete(*_TRACK_KEYS, "md:stream_status")
        await redis.aclose()


async def test_instruments_crud_and_validation(admin):
    c = admin.client
    page = (await c.get("/api/instruments", params={"query": "aapl"})).json()
    rows = page["items"]
    assert [r["symbol"] for r in rows] == ["AAPL"] and "provider_symbols" in rows[0]
    assert page["total"] == 1 and rows[0]["source"] == "catalog" and rows[0]["added_by"] is None
    crypto = (await c.get("/api/instruments", params={"asset_class": "crypto"})).json()["items"]
    assert crypto and all(r["asset_class"] == "crypto" for r in crypto)
    assert (await c.get("/api/instruments", params={"asset_class": "bonds"})).status_code == 400
    assert (await c.get("/api/instruments", params={"query": "%"})).json()["items"] == []
    paris = (await c.get("/api/instruments", params={"market": "EURONEXT_PARIS"})).json()
    assert paris["total"] >= 30 and all(r["market"] == "EURONEXT_PARIS" for r in paris["items"])
    assert (await c.get("/api/instruments", params={"market": "LSE"})).status_code == 400
    p1 = (await c.get("/api/instruments", params={"limit": 50})).json()
    p2 = (await c.get("/api/instruments", params={"limit": 50, "offset": 50})).json()
    assert p1["total"] == p2["total"] >= 400 and len(p1["items"]) == len(p2["items"]) == 50
    assert p1["items"][-1]["symbol"] < p2["items"][0]["symbol"]
    assert (await c.get("/api/instruments", params={"limit": 501})).status_code in (400, 422)

    new = {"symbol": "xyz", "name": "XYZ Corp", "asset_class": "us_equity", "market": "NYSE",
           "currency": "usd", "lot_size": "1", "tick_size": "0.01", "shortable": True}
    r = await c.post("/api/instruments", json=new)
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "XYZ" and r.json()["currency"] == "USD"
    assert r.json()["shortable"] is True and r.json()["active"] is True
    dup = await c.post("/api/instruments", json=new)
    assert dup.status_code == 400 and dup.json()["details"]["field"] == "symbol"
    for bad in (
        {"lot_size": "0"}, {"tick_size": "-1"}, {"asset_class": "bonds"}, {"market": "LSE"},
        {"currency": "US"}, {"symbol": "bad sym!"}, {"name": " "},
    ):
        r = await c.post("/api/instruments", json={**new, "symbol": "ABC", **bad})
        assert r.status_code == 400, bad

    r = await c.patch("/api/instruments/xyz", json={
        "name": "XYZ Inc", "active": False, "lot_size": "10", "tick_size": "0.05",
        "shortable": False, "provider_symbols": {"yahoo": "XYZ.X"}})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["name"], got["active"], got["lot_size"], got["tick_size"]) == (
        "XYZ Inc", False, "10", "0.05")
    assert got["provider_symbols"] == {"yahoo": "XYZ.X"}
    assert (await c.patch("/api/instruments/XYZ", json={"tick_size": "0"})).status_code == 400
    assert (await c.patch("/api/instruments/XYZ", json={"active": None})).status_code == 400
    assert (await c.patch("/api/instruments/NOPE", json={"active": True})).status_code == 404
    manual = (await c.get("/api/instruments", params={"source": "manual"})).json()["items"]
    assert [r["symbol"] for r in manual] == ["XYZ"]


async def test_instruments_auto_add_toggle(admin):
    c = admin.client
    assert (await c.get("/api/instruments/auto-add")).json() == {"on": True, "daily_limit": 25}
    r = await c.put("/api/instruments/auto-add", json={"on": False})
    assert r.status_code == 200 and r.json()["on"] is False
    assert (await c.get("/api/instruments/auto-add")).json()["on"] is False
    await c.put("/api/instruments/auto-add", json={"on": True})
    assert (await c.get("/api/instruments/auto-add")).json()["on"] is True
    assert (await c.put("/api/instruments/auto-add", json={"on": "x"})).status_code in (400, 422)


async def test_quotes_and_market_status(admin):
    c = admin.client
    r = await c.get("/api/quotes", params={"symbols": "AAPL,BTC-USD"})
    assert r.status_code == 200, r.text
    assert r.json()["AAPL"]["last"] == "200.05" and r.json()["BTC-USD"]["bid"] == "50000"
    assert (await c.get("/api/quotes", params={"symbols": ""})).status_code == 400
    many = ",".join(f"S{i}" for i in range(51))
    assert (await c.get("/api/quotes", params={"symbols": many})).status_code == 400
    r = await c.get("/api/quotes", params={"symbols": "AAPL,ZZZZ"})  # per-symbol fallback
    assert r.status_code == 200 and r.json()["AAPL"]["last"] == "200.05"
    assert "error" in r.json()["ZZZZ"]

    ms = (await c.get("/api/markets/status")).json()
    assert [m["market"] for m in ms] == [
        "NASDAQ", "NYSE", "MTA", "XETRA", "EURONEXT_PARIS", "CRYPTO", "FX"]
    crypto = next(m for m in ms if m["market"] == "CRYPTO")
    assert crypto["is_open"] is True


async def test_verify_chain_and_balances(admin):
    c = admin.client
    r = await c.get("/api/system/verify-chain")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["first_bad_journal_id"] is None
    r = await c.get("/api/system/verify-balances")
    assert r.json() == {"ok": True, "bad_accounts": []}


async def test_backup_download_streams_fake_dump(admin, monkeypatch, tmp_path):
    from smtrading.admin import routes_system

    payload = b"PGDMP-fake-" + b"x" * 200_000
    script = tmp_path / "fake_pg_dump.py"
    script.write_text(
        f"import sys\nsys.stdout.buffer.write({payload!r})\nsys.stderr.write('noise')\n"
    )
    import sys

    monkeypatch.setattr(
        routes_system, "dump_command", lambda: ([sys.executable, str(script)], {})
    )
    r = await admin.client.post("/api/system/backup")
    assert r.status_code == 200
    assert r.content == payload
    disp = r.headers["content-disposition"]
    import re

    assert re.fullmatch(r'attachment; filename="smtrading-\d{8}-\d{4}\.dump"', disp)
    assert r.headers["content-type"] == "application/octet-stream"


async def test_backup_failure_is_500(admin, monkeypatch, tmp_path):
    import sys

    from smtrading.admin import routes_system

    fail = tmp_path / "fail.py"
    fail.write_text("import sys\nsys.stderr.write('no server')\nsys.exit(1)\n")
    monkeypatch.setattr(routes_system, "dump_command", lambda: ([sys.executable, str(fail)], {}))
    r = await admin.client.post("/api/system/backup")
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    monkeypatch.setattr(routes_system, "dump_command", lambda: (["/nonexistent/pg_dump"], {}))
    assert (await admin.client.post("/api/system/backup")).status_code == 500


def test_dump_command_uses_database_url(monkeypatch):
    from smtrading.admin.routes_system import dump_command

    monkeypatch.setenv("SMT_DATABASE_URL", "postgresql+asyncpg://u:p%40ss@db:5433/mydb")
    monkeypatch.setenv("SMT_REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("SMT_MASTER_KEY", "k")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    try:
        argv, env = dump_command()
    finally:
        get_settings.cache_clear()
    assert argv[:2] == ["pg_dump", "--format=custom"]
    assert argv[argv.index("-h") + 1] == "db" and argv[argv.index("-p") + 1] == "5433"
    assert argv[argv.index("-U") + 1] == "u" and argv[argv.index("-d") + 1] == "mydb"
    assert env["PGPASSWORD"] == "p@ss" and "p@ss" not in " ".join(argv)


async def test_list_backups(admin, monkeypatch, tmp_path):
    from smtrading.admin import routes_system

    monkeypatch.setattr(routes_system, "BACKUP_DIR", tmp_path / "missing")
    assert (await admin.client.get("/api/system/backups")).json() == []
    monkeypatch.setattr(routes_system, "BACKUP_DIR", tmp_path)
    (tmp_path / "smtrading-2026-11-12.sql.gz").write_bytes(b"a" * 10)
    (tmp_path / "smtrading-2026-11-13.sql.gz").write_bytes(b"b" * 20)
    (tmp_path / "notes.txt").write_text("ignored")
    import os

    os.utime(tmp_path / "smtrading-2026-11-12.sql.gz", (1_000_000, 1_000_000))
    rows = (await admin.client.get("/api/system/backups")).json()
    assert [(r["name"], r["size_bytes"]) for r in rows] == [
        ("smtrading-2026-11-13.sql.gz", 20), ("smtrading-2026-11-12.sql.gz", 10)]


async def test_connectors_markets_system_require_admin(admin):
    c = admin.client
    c.cookies.clear()
    for method, url in (
        ("GET", "/api/connectors"), ("PATCH", "/api/connectors/yahoo"),
        ("POST", "/api/connectors/yahoo/test"), ("GET", "/api/provider-priorities"),
        ("PUT", "/api/provider-priorities"), ("GET", "/api/marketdata/stream-status"),
        ("POST", "/api/marketdata/track"), ("POST", "/api/marketdata/untrack"),
        ("GET", "/api/instruments"), ("POST", "/api/instruments"),
        ("PATCH", "/api/instruments/AAPL"), ("GET", "/api/quotes?symbols=AAPL"),
        ("GET", "/api/markets/status"), ("GET", "/api/system/verify-chain"),
        ("GET", "/api/system/verify-balances"), ("POST", "/api/system/backup"),
        ("GET", "/api/system/backups"),
    ):
        r = await c.request(method, url, json={} if method != "GET" else None)
        assert r.status_code == 401, url


async def test_nested_config_masked_and_preserved(admin, monkeypatch):
    from smtrading.connectors.service import get_connector_config

    await _add_fake_connector(admin, monkeypatch)
    c = admin.client
    config = {
        "auth": {"client_secret": "s3cr3t-value", "user": "bob"},
        "accounts": [{"api_key": "key-one-1111", "label": "a"}, {"api_key": "key-two-2222"}],
        "tokens": ["tok-aaaa-9999"],
    }
    r = await c.patch("/api/connectors/fake", json={"config": config})
    assert r.status_code == 200, r.text
    masked = r.json()["config_masked"]
    assert masked == {
        "auth": {"client_secret": "••••alue", "user": "bob"},
        "accounts": [{"api_key": "••••1111", "label": "a"}, {"api_key": "••••2222"}],
        "tokens": ["••••9999"],
    }
    assert "s3cr3t" not in json.dumps((await c.get("/api/connectors")).json())

    # sending the masked structure back keeps every stored secret
    r = await c.patch("/api/connectors/fake", json={"config": masked})
    assert r.status_code == 200, r.text
    async with admin.sm() as s:
        assert await get_connector_config(s, "fake") == config

    # a changed nested secret is stored; omitted secrets are kept, explicit null removes
    r = await c.patch("/api/connectors/fake", json={"config": {
        "auth": {"client_secret": "brand-new-secret", "user": "bob"},
        "accounts": [{"api_key": "••••1111", "label": "a"}, {"api_key": None}],
    }})
    assert r.status_code == 200, r.text
    async with admin.sm() as s:
        stored = await get_connector_config(s, "fake")
    assert stored["auth"]["client_secret"] == "brand-new-secret"
    assert stored["accounts"] == [{"api_key": "key-one-1111", "label": "a"}, {}]
    assert stored["tokens"] == ["tok-aaaa-9999"]  # omitted secret key kept


async def test_patch_merge_semantics_and_bad_mask(admin, monkeypatch):
    from smtrading.connectors.service import get_connector_config

    await _add_fake_connector(admin, monkeypatch)
    c = admin.client
    await c.patch("/api/connectors/fake", json={"config": {"api_key": "abcdef123456", "region": "eu"}})
    r = await c.patch("/api/connectors/fake", json={"config": {"region": "us"}})
    assert r.status_code == 200
    async with admin.sm() as s:
        assert await get_connector_config(s, "fake") == {"api_key": "abcdef123456", "region": "us"}
    r = await c.patch("/api/connectors/fake", json={"config": {"api_key": None}})
    async with admin.sm() as s:
        assert await get_connector_config(s, "fake") == {}
    # a masked value with nothing stored (or not matching) is refused
    r = await c.patch("/api/connectors/fake", json={"config": {"api_key": "••••3456"}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ORDER"
    await c.patch("/api/connectors/fake", json={"config": {"api_key": "abcdef123456"}})
    r = await c.patch("/api/connectors/fake", json={"config": {"api_key": "••••zzzz"}})
    assert r.status_code == 400
    async with admin.sm() as s:
        assert await get_connector_config(s, "fake") == {"api_key": "abcdef123456"}


async def test_dump_runs_with_minimal_env(admin, monkeypatch, tmp_path):
    import sys

    from smtrading.admin import routes_system

    monkeypatch.setenv("SOME_SECRET_ENV", "leak")
    script = tmp_path / "env_dump.py"
    script.write_text("import os, json, sys\nsys.stdout.write(json.dumps(sorted(os.environ)))\n")
    real = routes_system.dump_command
    monkeypatch.setattr(
        routes_system, "dump_command", lambda: ([sys.executable, str(script)], real()[1])
    )
    r = await admin.client.post("/api/system/backup")
    assert r.status_code == 200
    keys = set(json.loads(r.content))
    assert "SOME_SECRET_ENV" not in keys and "SMT_MASTER_KEY" not in keys
    assert {"PATH", "PGPASSWORD"} <= keys


async def test_backup_one_dump_at_a_time(admin, monkeypatch, tmp_path):
    import sys

    from smtrading.admin import routes_system

    slow = tmp_path / "slow_dump.py"
    slow.write_text(
        "import sys, time\nsys.stdout.buffer.write(b'PGDMP')\nsys.stdout.flush()\n"
        "time.sleep(1.5)\nsys.stdout.buffer.write(b'-end')\n"
    )
    monkeypatch.setattr(routes_system, "dump_command", lambda: ([sys.executable, str(slow)], {}))

    async def second():
        await asyncio.sleep(0.5)  # while the first dump streams
        return await admin.client.post("/api/system/backup")

    first, busy = await asyncio.gather(admin.client.post("/api/system/backup"), second())
    assert first.status_code == 200 and first.content == b"PGDMP-end"
    assert busy.status_code == 429
    assert busy.json() == {
        "code": "RATE_LIMITED", "message": "un backup è già in corso", "details": None,
    }
    again = await admin.client.post("/api/system/backup")  # the guard is released
    assert again.status_code == 200
    assert (await admin.client.get("/api/system/backup")).status_code == 405
