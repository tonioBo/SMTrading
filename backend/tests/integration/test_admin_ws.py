"""Admin WebSocket relay (/ws) and dashboard endpoints, against a real uvicorn server."""

import asyncio
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import uvicorn
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal
PASSWORD = "correct-horse-battery"


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NS:
    pass


@pytest.fixture
async def server(fresh_db, redis_url, monkeypatch):
    """A uvicorn server on the app, a logged-in httpx client (`client`, CSRF header set)
    and `redis` (a client on the same Redis)."""
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime

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
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis: McpRuntime(sm_, redis, qs, qs))
    port = _free_port()
    srv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        await asyncio.sleep(0.02)
    redis = Redis.from_url(redis_url)
    base = f"127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=f"http://{base}") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]
            ns = NS()
            ns.client, ns.sm, ns.redis, ns.host = client, sm, redis, base
            ns.ws_url = f"ws://{base}/ws"
            ns.cookie = f"smt_session={client.cookies['smt_session']}"
            yield ns
    finally:
        await redis.aclose()
        srv.should_exit = True
        thread.join(timeout=15)
        await engine.dispose()


async def _recv(ws, timeout: float = 5) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _closed_code(ws) -> int:
    with pytest.raises(ConnectionClosed) as exc:
        await asyncio.wait_for(ws.recv(), 5)
    return exc.value.rcvd.code


# --- WebSocket -----------------------------------------------------------------------


async def test_ws_without_cookie_closes_4401(server):
    async with connect(server.ws_url) as ws:
        assert await _closed_code(ws) == 4401


async def test_ws_with_bad_session_closes_4401(server):
    async with connect(server.ws_url, additional_headers={"Cookie": "smt_session=nope"}) as ws:
        assert await _closed_code(ws) == 4401


async def test_ws_foreign_origin_closes_4403(server):
    async with connect(
        server.ws_url, origin="http://evil.example", additional_headers={"Cookie": server.cookie}
    ) as ws:
        assert await _closed_code(ws) == 4403


async def test_ws_same_origin_relays_events_and_quotes(server):
    from smtrading.events import publish_event
    from smtrading.marketdata.cache import write_quote
    from smtrading.marketdata.types import Quote

    async with connect(
        server.ws_url, origin=f"http://{server.host}",
        additional_headers={"Cookie": server.cookie},
    ) as ws:
        await ws.send(json.dumps({"type": "subscribe_quotes", "symbols": [" aapl ", "AAPL"]}))
        # acknowledged once Redis confirmed (the `events` subscription came first)
        assert await _recv(ws) == {"type": "subscribed", "symbols": ["AAPL"]}

        await publish_event(server.redis, "kill_switch", {"on": True})
        msg = await _recv(ws)
        assert msg["type"] == "event"
        assert msg["event"]["type"] == "kill_switch" and msg["event"]["on"] is True

        now = datetime.now(UTC)
        await write_quote(server.redis, Quote("AAPL", D("200.1"), D("200.3"), D("200.2"), now, "t"))
        msg = await _recv(ws)
        assert msg["type"] == "quote"
        assert msg["quote"]["symbol"] == "AAPL" and msg["quote"]["bid"] == "200.1"

        await ws.send(json.dumps({"type": "unsubscribe_quotes", "symbols": ["aapl"]}))
        assert await _recv(ws) == {"type": "unsubscribed", "symbols": ["AAPL"]}
        later = now + timedelta(seconds=1)
        await write_quote(server.redis, Quote("AAPL", D("201"), D("202"), D("201.5"), later, "t"))
        await publish_event(server.redis, "marker", {})
        # the quote (published first) was not relayed: the next message is the event
        msg = await _recv(ws)
        assert msg["type"] == "event" and msg["event"]["type"] == "marker"


async def test_ws_rejects_malformed_messages_and_symbol_overflow(server):
    async with connect(server.ws_url, additional_headers={"Cookie": server.cookie}) as ws:
        await ws.send("not json")
        assert (await _recv(ws))["type"] == "error"
        await ws.send(json.dumps({"type": "nope"}))
        assert (await _recv(ws))["type"] == "error"
        await ws.send(json.dumps({"type": "subscribe_quotes", "symbols": [1]}))
        assert (await _recv(ws))["type"] == "error"
        too_many = [f"S{i}" for i in range(201)]
        await ws.send(json.dumps({"type": "subscribe_quotes", "symbols": too_many}))
        err = await _recv(ws)
        assert err["type"] == "error" and "200" in err["message"]
        await ws.send(json.dumps({"type": "subscribe_quotes", "symbols": too_many[:200]}))
        assert len((await _recv(ws))["symbols"]) == 200


async def test_ws_pings_and_closes_when_session_revoked(server, monkeypatch):
    from smtrading.admin import ws as ws_module

    monkeypatch.setattr(ws_module, "PING_INTERVAL_S", 0.2)
    async with connect(server.ws_url, additional_headers={"Cookie": server.cookie}) as ws:
        assert await _recv(ws) == {"type": "ping"}
        r = await server.client.post("/api/auth/logout")
        assert r.status_code == 204
        assert await _closed_code(ws) == 4401


# --- dashboard -----------------------------------------------------------------------


async def test_dashboard_requires_login(server):
    async with httpx.AsyncClient(base_url=server.client.base_url) as anon:
        assert (await anon.get("/api/dashboard")).status_code == 401
        assert (await anon.get("/api/dashboard/equity")).status_code == 401


async def test_dashboard_values_with_two_portfolios(server):
    from smtrading.connectors.models import Connector
    from smtrading.corporate.snapshots import local_date
    from smtrading.events import notify
    from smtrading.portfolios.service import create_portfolio
    from smtrading.risk.models import EquitySnapshot, SnapshotKind

    async with server.sm() as s:
        p1 = await create_portfolio(s, name="Alpha", initial_cash=D(10000), timezone="UTC")
        p2 = await create_portfolio(s, name="Beta", initial_cash=D(5000), timezone="UTC")
        s.add(Connector(name="zz_test", enabled=True, health_status="ok"))
        await s.commit()
        now = datetime.now(UTC)  # after the initial deposits: they are not P&L flows
        for p, equity in ((p1, D(9500)), (p2, D(5000))):
            s.add(EquitySnapshot(
                portfolio_id=p.id, ts=now, kind=SnapshotKind.day_open,
                local_date=local_date(p, now), equity_eur=equity, cash_eur=equity,
                exposure_eur=D(0), margin_used_eur=D(0),
            ))
        for i in range(12):
            await notify(s, portfolio_id=p1.id, kind="margin_warning", payload={"i": i})
        await notify(s, portfolio_id=p2.id, kind="liquidation", payload={"i": 99})
        await notify(s, portfolio_id=p2.id, kind="daily_loss", payload={})
        await s.commit()
    await server.redis.set("worker:heartbeat", str(time.time() - 5))

    r = await server.client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_equity_eur"] == "15000"
    assert body["day_pnl_eur"] == "500"
    assert body["day_pnl_pct"] == "3.4483"  # 500 / 14500
    by_name = {p["name"]: p for p in body["portfolios"]}
    assert set(by_name) == {"Alpha", "Beta"}
    assert by_name["Alpha"]["equity_eur"] == "10000"
    assert by_name["Alpha"]["day_pnl_pct"] == "5.2632"
    assert by_name["Alpha"]["status"] == "active" and by_name["Alpha"]["error"] is None
    assert by_name["Beta"]["day_pnl_pct"] == "0"
    assert {"name": "zz_test", "health_status": "ok"}.items() <= next(
        c for c in body["connectors"] if c["name"] == "zz_test"
    ).items()
    alerts = body["margin_alerts"]
    assert len(alerts) == 10
    assert {a["kind"] for a in alerts} <= {"margin_warning", "liquidation"}
    assert alerts[0]["kind"] == "liquidation"  # newest first
    assert body["kill_switch"] is False
    assert 4 <= body["worker_heartbeat_age_s"] < 60


async def test_dashboard_reports_unvaluable_portfolio_without_failing(server, monkeypatch):
    from smtrading.admin import routes_dashboard
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.portfolios.service import create_portfolio

    async with server.sm() as s:
        broken = await create_portfolio(s, name="Broken", initial_cash=D(100), timezone="UTC")
        await create_portfolio(s, name="Fine", initial_cash=D(200), timezone="UTC")
        await s.commit()
    real = routes_dashboard.value_portfolio

    async def flaky(session, qs, pid, *, redis=None):
        if pid == broken.id:
            raise SMTError(ErrorCode.STALE_QUOTE, "no quote")
        return await real(session, qs, pid, redis=redis)

    monkeypatch.setattr(routes_dashboard, "value_portfolio", flaky)
    body = (await server.client.get("/api/dashboard")).json()
    by_name = {p["name"]: p for p in body["portfolios"]}
    assert by_name["Broken"]["error"] == "STALE_QUOTE"
    assert by_name["Broken"]["equity_eur"] is None
    assert body["total_equity_eur"] == "200"
    assert body["day_pnl_pct"] is None  # no day_open taken


async def test_dashboard_equity_buckets_and_sums(server):
    from smtrading.portfolios.service import create_portfolio
    from smtrading.risk.models import EquitySnapshot, SnapshotKind

    epoch = datetime.now(UTC) - timedelta(minutes=30)
    t0 = datetime.fromtimestamp(epoch.timestamp() // 300 * 300, UTC)
    async with server.sm() as s:
        p1 = await create_portfolio(s, name="Alpha", initial_cash=D(1), timezone="UTC")
        p2 = await create_portfolio(s, name="Beta", initial_cash=D(1), timezone="UTC")

        def snap(p, minutes, equity, kind=SnapshotKind.periodic):
            s.add(EquitySnapshot(
                portfolio_id=p.id, ts=t0 + timedelta(minutes=minutes), kind=kind,
                local_date=None if kind == SnapshotKind.periodic else t0.date(),
                equity_eur=D(equity), cash_eur=D(equity), exposure_eur=D(0),
                margin_used_eur=D(0),
            ))

        snap(p1, 0, "10000")
        snap(p1, 1, "10100")  # same bucket: the last one wins
        snap(p2, 2, "5000")
        snap(p1, 0.5, "99999", SnapshotKind.day_open)  # not periodic: ignored
        snap(p1, 5, "10200.004")  # p2 missing here: carries 5000
        await s.commit()

    r = await server.client.get("/api/dashboard/equity", params={"period": "1d"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"] == "1d"
    b0, b1 = t0.isoformat(), (t0 + timedelta(minutes=5)).isoformat()
    assert body["series"] == [
        {"ts": b0, "equity_eur": "15100"}, {"ts": b1, "equity_eur": "15200"},
    ]
    alpha, beta = body["portfolios"]
    assert (alpha["name"], beta["name"]) == ("Alpha", "Beta")
    assert alpha["series"] == [
        {"ts": b0, "equity_eur": "10100"}, {"ts": b1, "equity_eur": "10200"},
    ]
    assert beta["series"] == [{"ts": b0, "equity_eur": "5000"}]

    bad = await server.client.get("/api/dashboard/equity", params={"period": "2d"})
    assert bad.status_code == 400 and bad.json()["details"]["field"] == "period"


async def test_dashboard_equity_caps_points(server):
    from smtrading.portfolios.service import create_portfolio
    from smtrading.risk.models import EquitySnapshot, SnapshotKind

    start = datetime.now(UTC) - timedelta(days=20)
    async with server.sm() as s:
        p = await create_portfolio(s, name="Many", initial_cash=D(1), timezone="UTC")
        for i in range(1200):
            s.add(EquitySnapshot(
                portfolio_id=p.id, ts=start + timedelta(minutes=5 * i),
                kind=SnapshotKind.periodic, equity_eur=D(i), cash_eur=D(i),
                exposure_eur=D(0), margin_used_eur=D(0),
            ))
        await s.commit()
    body = (await server.client.get("/api/dashboard/equity", params={"period": "1m"})).json()
    assert 0 < len(body["series"]) <= 500
    assert len(body["portfolios"][0]["series"]) <= 500
    assert body["series"][-1]["equity_eur"] == "1199"  # the last point is kept
