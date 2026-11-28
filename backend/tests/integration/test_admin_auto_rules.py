"""Admin API of the automatic rules (mode B): models, rules and their versions, activate /
pause / resume, the stream state, the trades, the results per rule and per day, the
backtest on one-minute history, the portfolio's brakes and the cleanup of a test
portfolio. Admin only (viewer 403), audited, never MCP."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import redis.asyncio as aioredis
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

D = Decimal
PASSWORD = "correct-horse-battery"
KEY = "KID-CANARY-7731"
SECRET = "SEC-CANARY-9902"
ACCT = "acct-id-canary-5520"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
SYMBOL = "AAPL"


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    """A logged-in admin client on a seeded database, with a real MarketDataRouter (Alpaca
    data connector on, keys set) for the backtest; `viewer()` opens a viewer client."""
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.connectors.service import update_connector
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.marketdata.router import MarketDataRouter
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
        await update_connector(s, "alpaca", enabled=True,
                               config={"key_id": KEY, "secret_key": SECRET})
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()
    redis = aioredis.from_url(redis_url)
    await redis.flushdb()

    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_fx("USD", "EUR", D("0.8"))
    http = httpx.AsyncClient()
    app = create_app(runtime_factory=lambda sm_, r: McpRuntime(
        sm_, r, qs, MarketDataRouter(sm_, r, http)))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    transport = httpx.ASGITransport(app=app)
    viewers: list[httpx.AsyncClient] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            async def viewer() -> httpx.AsyncClient:
                v_email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
                async with sm() as s:
                    s.add(AdminUser(email=v_email, password_hash=await hash_password(PASSWORD),
                                    role="viewer"))
                    await s.commit()
                c = httpx.AsyncClient(transport=transport, base_url="http://test")
                viewers.append(c)
                r = await c.post("/api/auth/login",
                                 json={"email": v_email, "password": PASSWORD})
                assert r.status_code == 200, r.text
                c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
                return c

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.redis, ns.email, ns.viewer = client, sm, redis, email, viewer
            yield ns
    finally:
        for c in viewers:
            await c.aclose()
        stop.set()
        await lifespan
        await http.aclose()
        await redis.aclose()
        await engine.dispose()


async def _portfolio(env, *, auto=True, agent=False, link=False) -> str:
    from smtrading.agents import service as agents
    from smtrading.agents.models import AgentPortfolio
    from smtrading.brokers.service import create_link
    from smtrading.portfolios.models import Portfolio

    r = await env.client.post("/api/portfolios", json={
        "name": f"P-{uuid.uuid4().hex[:8]}", "initial_cash": "10000"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    async with env.sm() as s:
        p = await s.get(Portfolio, uuid.UUID(pid))
        p.auto_rules_enabled = auto
        if link:
            bl = await create_link(s, f"Conto {uuid.uuid4().hex[:6]}", "alpaca_paper",
                                   {"key_id": KEY, "secret_key": SECRET,
                                    "account_id": ACCT})
            await s.flush()
            p.broker_link_id = bl.id
        if agent:
            a, _ = await agents.create_agent(s, f"agente-{uuid.uuid4().hex[:6]}")
            await s.flush()
            s.add(AgentPortfolio(agent_id=a.id, portfolio_id=p.id, permission="read"))
        await s.commit()
    return pid


def _rule_body(pid, **kw):
    body = {"portfolio_id": pid, "symbol": SYMBOL, "model": "breakout",
            "params": {"lookback_min": 10, "stop_pct": "0.4"}, "qty": "2"}
    body.update(kw)
    return body


async def _create(env, pid, **kw) -> dict:
    r = await env.client.post("/api/auto-rules", json=_rule_body(pid, **kw))
    assert r.status_code == 200, r.text
    return r.json()


async def _audits(env, method):
    from smtrading.agents.models import McpAuditLog

    async with env.sm() as s:
        return list(await s.scalars(
            select(McpAuditLog).where(McpAuditLog.method == method).order_by(McpAuditLog.id)))


def _refused(r, code="INVALID_ORDER", text=None):
    assert 400 <= r.status_code < 500, r.text
    body = r.json()
    err = body.get("error", body)
    assert err["code"] == code, body
    if text is not None:
        assert text in err["message"], err["message"]
    return err


async def _trade(env, rule_id, pid, *, key, at, status="closed", net="1", fees="0.5",
                 ms=(10, 20, 30), slip="2", version=1):
    from smtrading.autorules.tables import AutoTrade

    async with env.sm() as s:
        t = AutoTrade(
            rule_id=uuid.UUID(rule_id), rule_version=version, portfolio_id=uuid.UUID(pid),
            symbol=SYMBOL, trigger_key=key, trigger_at=at, trigger_price=D(100),
            trigger_values={"high": "99.5"}, expected_price=D(100), status=status,
            fill_price=D(100), qty=D(2), fees=D(fees), fees_currency="USD",
            net_result=None if net is None else D(net), net_currency="EUR",
            exit_filled_at=at + timedelta(minutes=2) if status == "closed" else None,
            entry_slippage_bps=None if slip is None else D(slip),
            ms_trigger_to_send=ms[0], ms_send_to_broker=ms[1], ms_broker_to_fill=ms[2])
        s.add(t)
        await s.commit()
        return t.id


# --- admin only ---------------------------------------------------------------------


async def test_auto_rules_admin_only(env):
    pid = await _portfolio(env)
    rule = await _create(env, pid)
    v = await env.viewer()
    rid = rule["id"]
    writes = [
        ("POST", "/api/auto-rules", _rule_body(pid)),
        ("PATCH", f"/api/auto-rules/{rid}", {"qty": "3"}),
        ("POST", f"/api/auto-rules/{rid}/activate", None),
        ("POST", f"/api/auto-rules/{rid}/pause", None),
        ("POST", f"/api/auto-rules/{rid}/resume", None),
        ("POST", "/api/auto-rules/backtest", {
            "symbol": SYMBOL, "model": "breakout", "params": {}, "qty": "1",
            "from": "2026-11-02", "to": "2026-11-03", "fee_rule": "zero"}),
        ("DELETE", f"/api/portfolios/{pid}/auto-rules", None),
    ]
    for method, url, body in writes:
        r = await v.request(method, url, json=body)
        assert r.status_code == 403, (method, url, r.text)
    for url in ("/api/auto-rules/models", "/api/auto-rules", f"/api/auto-rules/{rid}/trades",
                "/api/auto-rules/results"):
        r = await v.get(url)
        assert r.status_code == 403, (url, r.text)
    # nothing changed by the viewer
    r = await env.client.get("/api/auto-rules", params={"portfolio_id": pid})
    assert [x["version"] for x in r.json()] == [1]


async def test_models_catalog_serialised(env):
    r = await env.client.get("/api/auto-rules/models")
    assert r.status_code == 200, r.text
    models = {m["name"]: m for m in r.json()}
    assert set(models) == {"breakout", "mean_reversion", "vwap_pullback"}
    stop = next(p for p in models["breakout"]["params"] if p["name"] == "stop_pct")
    assert stop == {"name": "stop_pct", "kind": "dec", "lo": "0.05", "hi": "5",
                    "default": "0.3", "choices": [], "common": True}
    look = next(p for p in models["breakout"]["params"] if p["name"] == "lookback_min")
    assert look["lo"] == 5 and look["default"] == 20


# --- create, version, activate ------------------------------------------------------


async def test_create_update_activate_flow(env):
    pid = await _portfolio(env)
    rule = await _create(env, pid, brakes={"max_trades_hour": 5})
    assert rule["status"] == "draft" and rule["status_it"] == "bozza"
    assert rule["version"] == 1 and rule["qty"] == "2" and rule["notional"] is None
    assert rule["params"]["stop_pct"] == "0.4" and rule["params"]["lookback_min"] == 10
    assert rule["brakes"]["max_trades_hour"] == 5
    assert rule["portfolio_name"].startswith("P-") and rule["open_trade"] is False
    assert rule["today"] == {"trades": 0, "net": "0", "net_currency": "EUR"}
    rid = rule["id"]

    # a cooldown of an earlier refusal is cleared by the new version
    await env.redis.set(f"autorules:rejected:{rid}", "1")
    r = await env.client.patch(f"/api/auto-rules/{rid}",
                               json={"notional": "500", "params": {"stop_pct": "0.6"}})
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["notional"] == "500" and v2["qty"] is None
    assert v2["params"]["stop_pct"] == "0.6" and v2["brakes"]["max_trades_hour"] == 5
    assert await env.redis.get(f"autorules:rejected:{rid}") is None

    await env.redis.set(f"autorules:rejected:{rid}", "1")
    r = await env.client.post(f"/api/auto-rules/{rid}/activate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active" and r.json()["status_it"] == "attiva"
    assert await env.redis.get(f"autorules:rejected:{rid}") is None
    assert SYMBOL.encode() in await env.redis.smembers("md:tracked:auto_rules")

    r = await env.client.patch(f"/api/auto-rules/{rid}", json={"qty": "1"})
    _refused(r, text="metti in pausa la regola")

    r = await env.client.post(f"/api/auto-rules/{rid}/pause", json={"reason": "prova"})
    assert r.status_code == 200, r.text
    assert r.json()["status_it"] == "in pausa" and r.json()["status_reason"] == "prova"
    await env.redis.set(f"autorules:rejected:{rid}", "1")
    r = await env.client.post(f"/api/auto-rules/{rid}/resume")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert await env.redis.get(f"autorules:rejected:{rid}") is None

    for action, status in (("create", "draft"), ("update", "draft"), ("activate", "active"),
                           ("pause", "paused"), ("resume", "active")):
        rows = await _audits(env, f"admin:auto_rules.{action}")
        assert len(rows) == 1, action
        assert rows[0].params["by"] == env.email and rows[0].params["rule_id"] == rid
        assert rows[0].params["status"] == status
    assert (await _audits(env, "admin:auto_rules.update"))[0].params["version"] == 2

    r = await env.client.get("/api/auto-rules", params={"portfolio_id": pid})
    assert [x["id"] for x in r.json()] == [rid]
    r = await env.client.get("/api/auto-rules", params={"portfolio_id": str(uuid.uuid4())})
    assert r.json() == []


async def test_float_params_refused_strings_accepted(env):
    pid = await _portfolio(env)
    r = await env.client.post("/api/auto-rules", json=_rule_body(
        pid, params={"stop_pct": 0.4}))
    _refused(r, text="stop_pct")
    assert "testo" in r.json()["message"]
    r = await env.client.post("/api/auto-rules", json=_rule_body(
        pid, brakes={"max_loss_day": 12.5}))
    _refused(r, text="max_loss_day")
    r = await env.client.post("/api/auto-rules", json=_rule_body(pid, qty=1.5))
    assert r.status_code in (400, 422), r.text
    r = await env.client.post("/api/auto-rules", json=_rule_body(
        pid, params={"stop_pct": 1}, brakes={"max_loss_day": "12.5"}))
    assert r.status_code == 200, r.text
    assert r.json()["params"]["stop_pct"] == "1"
    assert r.json()["brakes"]["max_loss_day"] == "12.5"


async def test_activate_errors_in_italian(env):
    pid = await _portfolio(env, auto=False)
    rule = await _create(env, pid)
    r = await env.client.post(f"/api/auto-rules/{rule['id']}/activate")
    _refused(r, text="le regole automatiche sono spente su questo portafoglio")
    r = await env.client.post(f"/api/auto-rules/{rule['id']}/pause")
    _refused(r, text="la regola è una bozza")
    r = await env.client.post(f"/api/auto-rules/{uuid.uuid4()}/activate")
    _refused(r, code="NOT_FOUND", text="regola non trovata")
    r = await env.client.post("/api/auto-rules", json=_rule_body(pid, model="nessuno"))
    _refused(r, text="modello sconosciuto")
    assert await _audits(env, "admin:auto_rules.activate") == []


async def test_stream_state_in_dto(env):
    from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY

    pid = await _portfolio(env)
    rule = await _create(env, pid)
    r = await env.client.get("/api/auto-rules", params={"portfolio_id": pid})
    assert (r.json()[0]["stream"], r.json()[0]["stream_it"]) == ("none", "senza flusso")
    await env.redis.set(STATUS_KEY, json.dumps({
        "streams": {"alpaca": {"state": CONNECTED, "symbols": ["MSFT"]}},
        "polling": [SYMBOL]}))
    r = await env.client.get("/api/auto-rules", params={"portfolio_id": pid})
    assert (r.json()[0]["stream"], r.json()[0]["stream_it"]) == ("polling", "in polling")
    await env.redis.set(STATUS_KEY, json.dumps({
        "streams": {"alpaca": {"state": CONNECTED, "symbols": [SYMBOL]}}, "polling": []}))
    r = await env.client.post(f"/api/auto-rules/{rule['id']}/activate")
    assert (r.json()["stream"], r.json()["stream_it"]) == ("stream", "con flusso")


# --- trades and results ---------------------------------------------------------------


async def test_trades_listed_with_numbers_as_strings(env):
    pid = await _portfolio(env)
    rule = await _create(env, pid)
    now = datetime.now(UTC)
    await _trade(env, rule["id"], pid, key="a", at=now - timedelta(minutes=30))
    await _trade(env, rule["id"], pid, key="b", at=now - timedelta(minutes=10), status="open",
                 net=None)
    r = await env.client.get(f"/api/auto-rules/{rule['id']}/trades")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [t["trigger_key"] for t in rows] == ["b", "a"]  # newest first
    assert rows[1]["net_result"] == "1" and rows[1]["fees"] == "0.5"
    assert rows[1]["trigger_price"] == "100" and rows[1]["trigger_values"] == {"high": "99.5"}
    assert rows[1]["ms_trigger_to_send"] == 10
    r = await env.client.get(f"/api/auto-rules/{rule['id']}/trades", params={"limit": 1})
    assert len(r.json()) == 1
    _refused(await env.client.get(f"/api/auto-rules/{rule['id']}/trades",
                                  params={"limit": 501}))
    r = await env.client.get("/api/auto-rules", params={"portfolio_id": pid})
    dto = r.json()[0]
    assert dto["open_trade"] is True
    assert dto["today"]["trades"] == 1 and dto["today"]["net"] == "1"


async def test_results_per_rule_and_day(env):
    pid = await _portfolio(env)
    rule = await _create(env, pid)
    rid = rule["id"]
    # the counters live 3 days: yesterday and today
    d2 = datetime.now(UTC).replace(hour=0, minute=1, second=0, microsecond=0)
    d1 = d2 - timedelta(days=1) + timedelta(hours=15)
    day1, day2 = d1.date().isoformat(), d2.date().isoformat()
    await _trade(env, rid, pid, key="1", at=d1, net="3", fees="0.5", ms=(10, 100, 7), slip="2")
    await _trade(env, rid, pid, key="2", at=d1 + timedelta(minutes=5), net="-1", fees="0.5",
                 ms=(20, 200, 9), slip="3")
    await _trade(env, rid, pid, key="3", at=d1 + timedelta(minutes=9), net="2", fees="0.25",
                 ms=(40, 300, 11), slip=None)
    await _trade(env, rid, pid, key="4", at=d2, net="-4", fees="1", ms=(5, 50, 1), slip="-1",
                 version=2)
    await _trade(env, rid, pid, key="5", at=d2 + timedelta(minutes=1), status="rejected",
                 net=None, fees="0", ms=(15, 60, 3), slip=None, version=2)
    await env.redis.hset(f"autorules:blocked:{rid}:{d1:%Y%m%d}",
                         mapping={"stale": 2, "spread": 1})
    old = d2 - timedelta(days=10)  # older than the counters' life: not looked up
    await env.redis.hset(f"autorules:blocked:{rid}:{old:%Y%m%d}", "stale", 5)
    r = await env.client.get("/api/auto-rules/results", params={
        "portfolio_id": pid, "from": (old - timedelta(days=1)).date().isoformat(),
        "to": day2})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(x["rule_id"], x["day"]) for x in rows] == [(rid, day1), (rid, day2)]
    one, two = rows
    assert (one["version"], one["trades"], one["wins"], one["losses"]) == (1, 3, 2, 1)
    assert one["net"] == "4" and one["fees"] == {"USD": "1.25"}
    assert one["median_ms_trigger_to_send"] == "20"
    assert one["median_ms_send_to_broker"] == "200"
    assert one["median_ms_broker_to_fill"] == "9"
    assert one["avg_entry_slippage_bps"] == "2.5"
    assert one["blocked"] == {"stale": 2, "spread": 1}
    assert (two["version"], two["trades"], two["wins"], two["losses"]) == (2, 1, 0, 1)
    assert two["net"] == "-4" and two["fees"] == {"USD": "1"}
    assert two["median_ms_trigger_to_send"] == "10"  # even count: the middle two's mean
    assert two["avg_entry_slippage_bps"] == "-1" and two["blocked"] == {}
    _refused(await env.client.get("/api/auto-rules/results", params={
        "from": "2026-11-05", "to": "2026-11-01"}))


# --- backtest -------------------------------------------------------------------------


def _alpaca_bars(day: datetime, n: int = 40) -> list[dict]:
    """A flat session start at 100, then a breakout bar and a bar reaching the target."""
    out = []
    open_ = day.replace(hour=14, minute=30)
    for i in range(n):
        ts = open_ + timedelta(minutes=i)
        if i == 20:
            o, h, l, c = 100, 101.2, 100, 101.2
        elif i == 21:
            o, h, l, c = 101.2, 102.5, 101.2, 102.4
        else:
            o = h = l = c = 100
        out.append({"t": ts.isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l,
                    "c": c, "v": 1000, "n": 5, "vw": 100})
    return out


async def test_backtest_endpoint_limits(env):
    body = {"symbol": SYMBOL, "model": "breakout", "params": {"lookback_min": 10},
            "qty": "10", "from": "2026-10-01", "to": "2026-10-31", "fee_rule": "zero"}
    r = await env.client.post("/api/auto-rules/backtest", json=body)
    _refused(r, code="RISK_LIMIT_EXCEEDED", text="30 giorni")
    r = await env.client.post("/api/auto-rules/backtest", json={
        **body, "symbol": None, "symbols": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA"],
        "from": "2026-11-02", "to": "2026-11-03"})
    _refused(r, code="RISK_LIMIT_EXCEEDED", text="5 simboli")
    r = await env.client.post("/api/auto-rules/backtest", json={
        **body, "from": "2026-11-02", "to": "2026-11-03", "fee_rule": "broker"})
    _refused(r, text="portafoglio o il collegamento")

    pid = await _portfolio(env)
    d1, d2 = datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.get(BARS_URL).mock(return_value=httpx.Response(200, json={
            "bars": {SYMBOL: _alpaca_bars(d1) + _alpaca_bars(d2)}, "next_page_token": None}))
        r = await env.client.post("/api/auto-rules/backtest", json={
            **body, "from": "2026-11-02", "to": "2026-11-03", "fee_rule": "broker",
            "portfolio_id": pid, "spread_pct": "0", "delay_s": 0})
        assert r.status_code == 200, r.text
        assert route.called
        assert all(c.request.url.params["timeframe"] == "1Min" for c in route.calls)
        assert KEY not in json.dumps(r.json()) and SECRET not in json.dumps(r.json())
        got = r.json()
        assert got["symbols"] == [SYMBOL]
        assert any("al minuto non si vedono" in n for n in got["notes"])
        assert len(got["trades"]) == 2 and got["wins"] == 2 and got["losses"] == 0
        assert {t["exit_reason"] for t in got["trades"]} == {"target"}
        # the seeded global rule: 1 EUR fixed + 0.1%, at least 1, on entry and on exit
        assert D(got["fees"]) > 0 and got["trades"][0]["symbol"] == SYMBOL
        assert D(got["net"]) == sum(D(t["net"]) for t in got["trades"])
        assert got["per_symbol"][0]["symbol"] == SYMBOL

        r = await env.client.post("/api/auto-rules/backtest", json={
            **body, "from": "2026-11-02", "to": "2026-11-03", "spread_pct": "0",
            "delay_s": 0})
        assert r.status_code == 200, r.text
        assert D(r.json()["fees"]) == 0 and len(r.json()["trades"]) == 2


def _fake_result(n: int):
    from smtrading.autorules.replay import ReplayResult, ReplayTrade

    t0 = datetime(2026, 11, 2, 15, tzinfo=UTC)
    trades = [ReplayTrade(t0 + timedelta(minutes=i), D(100), t0 + timedelta(minutes=i, seconds=30),
                          D(101), "target", D(1), D(0), D(1)) for i in range(n)]
    return ReplayResult(trades, D(n), D(0), n, 0, D(0), ["nota"])


def _backtest_body():
    return {"symbol": SYMBOL, "model": "breakout", "params": {}, "qty": "1",
            "from": "2026-11-02", "to": "2026-11-02", "fee_rule": "zero"}


async def test_backtest_does_not_block_the_loop_and_runs_one_at_a_time(env, monkeypatch):
    import time as _time

    from smtrading.admin import routes_auto_rules

    def slow_replay(*a, **kw):
        _time.sleep(1.5)  # CPU-bound work stand-in: blocks its thread, not the loop
        return _fake_result(3)

    monkeypatch.setattr(routes_auto_rules, "replay", slow_replay)
    body = _backtest_body()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(BARS_URL).mock(return_value=httpx.Response(200, json={
            "bars": {SYMBOL: _alpaca_bars(datetime(2026, 11, 2, tzinfo=UTC))},
            "next_page_token": None}))
        long = asyncio.create_task(env.client.post("/api/auto-rules/backtest", json=body))
        await asyncio.sleep(0.5)  # the replay is running
        t0 = _time.monotonic()
        health = await env.client.get("/health")
        # answered while the replay runs (503 here: no worker heartbeat in the test)
        assert health.status_code in (200, 503) and _time.monotonic() - t0 < 0.8
        assert not long.done()
        busy = await env.client.post("/api/auto-rules/backtest", json=body)
        _refused(busy, code="RATE_LIMITED", text="già in corso")
        r = await long
        assert r.status_code == 200, r.text
        assert r.json()["trades_total"] == 3 and r.json()["truncated"] is False
        # the slot is free again
        r = await env.client.post("/api/auto-rules/backtest", json=body)
        assert r.status_code == 200, r.text


async def test_backtest_trades_truncated(env, monkeypatch):
    from smtrading.admin import routes_auto_rules

    monkeypatch.setattr(routes_auto_rules, "replay", lambda *a, **kw: _fake_result(600))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(BARS_URL).mock(return_value=httpx.Response(200, json={
            "bars": {SYMBOL: _alpaca_bars(datetime(2026, 11, 2, tzinfo=UTC))},
            "next_page_token": None}))
        r = await env.client.post("/api/auto-rules/backtest", json=_backtest_body())
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["truncated"] is True and got["trades_total"] == 600
    assert len(got["trades"]) == routes_auto_rules.MAX_RETURNED_TRADES
    assert got["net"] == "600" and got["wins"] == 600  # the totals count every trade
    # the newest kept: trades 100..599
    assert got["trades"][0]["entry_ts"] == "2026-11-02T16:40:00+00:00"
    assert got["trades"][-1]["entry_ts"] == "2026-11-03T00:59:00+00:00"


async def test_backtest_fees_from_the_link_preset(env):
    from smtrading.brokers.service import create_link

    async with env.sm() as s:
        link = await create_link(s, f"Conto {uuid.uuid4().hex[:6]}", "alpaca_paper",
                                 {"key_id": KEY, "secret_key": SECRET})
        await s.commit()
    d1 = datetime(2026, 11, 2, tzinfo=UTC)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(BARS_URL).mock(return_value=httpx.Response(200, json={
            "bars": {SYMBOL: _alpaca_bars(d1)}, "next_page_token": None}))
        r = await env.client.post("/api/auto-rules/backtest", json={
            "symbol": SYMBOL, "model": "breakout", "params": {"lookback_min": 10},
            "qty": "10", "from": "2026-11-02", "to": "2026-11-02", "fee_rule": "broker",
            "broker_link_id": str(link.id), "spread_pct": "0", "delay_s": 0})
    assert r.status_code == 200, r.text
    # Alpaca stocks: zero commission
    assert len(r.json()["trades"]) == 1 and D(r.json()["fees"]) == 0


# --- portfolio brakes -----------------------------------------------------------------


async def test_portfolio_brakes_patch(env):
    from smtrading.portfolios.models import Portfolio

    pid = await _portfolio(env)
    r = await env.client.patch(f"/api/portfolios/{pid}",
                               json={"auto_brakes": {"max_trades_hour": 500}})
    _refused(r, text="max_trades_hour")
    r = await env.client.patch(f"/api/portfolios/{pid}",
                               json={"auto_brakes": {"max_loss_day": 10.5}})
    _refused(r, text="testo")
    r = await env.client.patch(f"/api/portfolios/{pid}", json={
        "auto_brakes": {"max_trades_hour": 30, "max_loss_day": "250", "max_open_positions": 3}})
    assert r.status_code == 200, r.text
    brakes = r.json()["auto_brakes"]
    assert brakes["max_trades_hour"] == 30 and brakes["max_loss_day"] == "250"
    assert brakes["max_open_positions"] == 3 and brakes["max_trades_day"] == 300
    async with env.sm() as s:
        assert (await s.get(Portfolio, uuid.UUID(pid))).auto_brakes == brakes
    audit = (await _audits(env, "admin:portfolio.intraday"))[-1]
    assert audit.params["changes"]["auto_brakes"] == brakes
    assert audit.params["by"] == env.email
    r = await env.client.get(f"/api/portfolios/{pid}")
    assert r.json()["auto_brakes"] == brakes
    v = await env.viewer()
    r = await v.patch(f"/api/portfolios/{pid}", json={"auto_brakes": {}})
    assert r.status_code == 403


# --- cleanup --------------------------------------------------------------------------


async def test_purge_refuses_and_deletes(env):
    from smtrading.autorules.tables import AutoRule, AutoRuleVersion, AutoTrade

    shared = await _portfolio(env, agent=True)
    rule = await _create(env, shared)
    r = await env.client.delete(f"/api/portfolios/{shared}/auto-rules")
    _refused(r, text="agenti")

    from datetime import date

    from smtrading.campaigns.models import Campaign, CampaignPortfolio

    in_campaign = await _portfolio(env)
    await _create(env, in_campaign)
    async with env.sm() as s:
        c = Campaign(name=f"C-{uuid.uuid4().hex[:6]}", start_date=date(2026, 11, 1),
                     end_date=date(2026, 12, 23))
        s.add(c)
        await s.flush()
        s.add(CampaignPortfolio(campaign_id=c.id, portfolio_id=uuid.UUID(in_campaign)))
        await s.commit()
    r = await env.client.delete(f"/api/portfolios/{in_campaign}/auto-rules")
    _refused(r, text="campagna")

    pid = await _portfolio(env)
    first = await _create(env, pid)
    second = await _create(env, pid, symbol="MSFT")
    r = await env.client.patch(f"/api/auto-rules/{first['id']}", json={"qty": "3"})
    assert r.status_code == 200
    r = await env.client.post(f"/api/auto-rules/{first['id']}/activate")
    assert r.status_code == 200, r.text
    r = await env.client.delete(f"/api/portfolios/{pid}/auto-rules")
    _refused(r, text="attiva")
    r = await env.client.post(f"/api/auto-rules/{first['id']}/pause")
    assert r.status_code == 200
    now = datetime.now(UTC)
    await _trade(env, first["id"], pid, key="open", at=now, status="open", net=None)
    r = await env.client.delete(f"/api/portfolios/{pid}/auto-rules")
    _refused(r, text="aperte")
    async with env.sm() as s:
        t = await s.scalar(select(AutoTrade).where(AutoTrade.trigger_key == "open"))
        t.status = "closed"
        await s.commit()
    await _trade(env, second["id"], pid, key="x", at=now)
    for rid in (first["id"], second["id"]):
        await env.redis.hset(f"autorules:blocked:{rid}:20261102", "stale", 1)
        await env.redis.hset(f"autorules:blocked:{rid}:{now:%Y%m%d}", "spread", 1)
    other = f"autorules:blocked:{rule['id']}:20261102"
    await env.redis.hset(other, "stale", 1)
    await env.redis.xadd(f"bars1m:{SYMBOL}", {"c": "1"})
    await env.redis.sadd("md:tracked:auto_rules", SYMBOL)

    r = await env.client.delete(f"/api/portfolios/{pid}/auto-rules")
    assert r.status_code == 200, r.text
    assert r.json() == {"rules": 2, "trades": 2}
    async with env.sm() as s:
        ids = [uuid.UUID(first["id"]), uuid.UUID(second["id"])]
        assert await s.scalar(select(func.count()).select_from(AutoRule)
                              .where(AutoRule.id.in_(ids))) == 0
        assert await s.scalar(select(func.count()).select_from(AutoRuleVersion)
                              .where(AutoRuleVersion.rule_id.in_(ids))) == 0
        assert await s.scalar(select(func.count()).select_from(AutoTrade)
                              .where(AutoTrade.portfolio_id == uuid.UUID(pid))) == 0
        # the other portfolio's rule is untouched
        assert await s.get(AutoRule, uuid.UUID(rule["id"])) is not None
    assert await env.redis.smembers("md:tracked:auto_rules") == set()
    for rid in (first["id"], second["id"]):
        assert [k async for k in env.redis.scan_iter(match=f"autorules:blocked:{rid}:*")] == []
    assert await env.redis.exists(other) == 1
    assert await env.redis.exists(f"bars1m:{SYMBOL}") == 0
    audit = (await _audits(env, "admin:auto_rules.purge"))[-1]
    assert audit.params["by"] == env.email and audit.params["rules"] == 2


async def test_purge_cli(env, capsys):
    from smtrading import cli

    pid = await _portfolio(env)
    await _create(env, pid)
    code, out = await cli.run_command(
        cli.build_parser().parse_args(["autorules", "purge", "--portfolio", pid]),
        env.sm, None, env.redis)
    assert code == 0, out
    assert json.loads(out) == {"rules": 1, "trades": 0}
    assert (await _audits(env, "admin:auto_rules.purge"))[-1].params["by"] == "cli"


# --- no secrets, MCP unchanged ----------------------------------------------------------


async def test_no_secret_in_dto(env):
    pid = await _portfolio(env, link=True)
    rule = await _create(env, pid)
    await _trade(env, rule["id"], pid, key="a", at=datetime.now(UTC))
    bodies = [
        (await env.client.get("/api/auto-rules", params={"portfolio_id": pid})).text,
        (await env.client.get(f"/api/auto-rules/{rule['id']}/trades")).text,
        (await env.client.get("/api/auto-rules/results", params={"portfolio_id": pid})).text,
        (await env.client.patch(f"/api/auto-rules/{rule['id']}", json={"qty": "4"})).text,
    ]
    for text in bodies:
        for secret in (KEY, SECRET, ACCT, "key_id", "account_id", "secret"):
            assert secret not in text, (secret, text)


async def test_mcp_unchanged_after_routes():
    """The routes are admin only: the MCP catalog (test_mcp_catalog_frozen) is the same,
    and no tool mentions the automatic rules."""
    from smtrading.mcp.catalog import catalog_fingerprint
    from smtrading.mcp.server import build_mcp

    mcp = build_mcp()
    assert await catalog_fingerprint(mcp) == (
        "668d1432822dc806bbd87a5ed6f5cf9c89130d988d5fa56723789575b88237b9")
    names = [t.name for t in await mcp.list_tools()]
    assert not [n for n in names if "auto" in n or "rule" in n]
