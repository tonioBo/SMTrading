"""Admin API «Segnali» (GET /api/signals) and «Posizioni» (GET /api/positions/open): the signal engine grouped by portfolio and
strategy, numbers as strings, cached 60 s in redis (one computation per minute however
many tabs are open); a viewer can read it."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

PASSWORD = "correct-horse-battery"
D = Decimal


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.admin.routes_signals import CACHE_KEY
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
    redis = Redis.from_url(redis_url)
    await redis.delete(CACHE_KEY)

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()

    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
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
            assert r.status_code == 200, r.text

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.qs, ns.redis = client, sm, qs, redis
            yield ns
    finally:
        stop.set()
        await lifespan
        await redis.delete(CACHE_KEY)
        await redis.aclose()
        await engine.dispose()


async def _strategy(admin, symbols):
    from smtrading.activity.models import Decision
    from smtrading.agents import service as agents
    from smtrading.portfolios.service import create_portfolio

    async with admin.sm() as s:
        p = await create_portfolio(s, name="Portafoglio Prudente", initial_cash=D(500))
        ceo, _ = await agents.create_agent(s, "CEO")
        s.add(Decision(agent_id=ceo.id, kind="strategy", summary="Approvata STR-001",
                       portfolio_id=p.id, symbols=symbols, details={
                           "ref": "STR-001", "status": "approvata", "timeframe": "1d",
                           "strategy": "rsi_reversion",
                           "params": {"period": 2, "buy_below": 10, "sell_above": 70}}))
        await s.commit()
        return p


async def test_empty(admin):
    r = await admin.client.get("/api/signals")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["portfolios"] == [] and datetime.fromisoformat(body["as_of"])


async def test_shape_and_cache(admin):
    from smtrading.marketdata.types import Bar

    p = await _strategy(admin, ["BTC-USD"])  # crypto: always open
    day = timedelta(days=1)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    admin.qs.set_bars("BTC-USD", [Bar(today - day * (40 - i), D(c), D(c + 1), D(c - 1),
                                      D(c), D(1)) for i, c in enumerate(range(100, 140))])
    admin.qs.set_quote("BTC-USD", D(99), D(101), None)  # mid 100: a sharp drop

    r = await admin.client.get("/api/signals")
    assert r.status_code == 200, r.text
    body = r.json()
    [port] = body["portfolios"]
    assert port["portfolio_id"] == str(p.id)
    assert port["portfolio_name"] == "Portafoglio Prudente"
    [st] = port["strategies"]
    assert {k: st[k] for k in ("ref", "name", "type", "timeframe", "status")} == {
        "ref": "STR-001", "name": "Approvata STR-001", "type": "rsi_reversion",
        "timeframe": "1d", "status": "ok"}
    assert st["params"] == {"period": 2, "buy_below": "10", "sell_above": "70"}
    [x] = st["symbols"]
    assert set(x) == {"symbol", "last", "indicator", "value_closed", "value_now",
                      "threshold", "direction", "distance", "triggered_closed",
                      "triggered_now", "as_of", "position", "open_entry"}
    assert x["position"] is None and x["open_entry"] is False
    assert x["symbol"] == "BTC-USD" and x["last"] == "100" and x["direction"] == "below"
    assert D(x["value_closed"]) == 100 and x["triggered_closed"] is False
    assert D(x["value_now"]) < 10 and x["triggered_now"] is True
    assert x["threshold"] == "10" and D(x["distance"]) < 0
    assert isinstance(x["value_now"], str) and datetime.fromisoformat(x["as_of"])

    # cached: a second tab within the minute gets the same answer, no new computation
    admin.qs.set_quote("BTC-USD", D(199), D(201), None)
    again = (await admin.client.get("/api/signals")).json()
    assert again == body
    assert 0 < await admin.redis.ttl("signals:v1") <= 60


async def test_signals_show_the_position(admin):
    from smtrading.marketdata.types import Bar
    from smtrading.positions.models import Position

    p = await _strategy(admin, ["BTC-USD"])
    day = timedelta(days=1)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    admin.qs.set_bars("BTC-USD", [Bar(today - day * (40 - i), D(c), D(c + 1), D(c - 1),
                                      D(c), D(1)) for i, c in enumerate(range(100, 140))])
    admin.qs.set_quote("BTC-USD", D(99), D(101), None)
    async with admin.sm() as s:
        s.add(Position(portfolio_id=p.id, symbol="BTC-USD", qty=D("0.5"), avg_price=D(120)))
        await s.commit()
    body = (await admin.client.get("/api/signals")).json()
    [x] = body["portfolios"][0]["strategies"][0]["symbols"]
    assert x["position"] == {"qty": "0.5", "avg_price": "120"}
    assert x["triggered_now"] is True  # the values stay


async def test_open_positions_route(admin):
    from smtrading.engine.execution import execute_fill
    from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
    from smtrading.portfolios.service import create_portfolio

    admin.qs.set_quote("BTC-USD", D(100), D(100), D(100))
    admin.qs.set_fx("EUR", "USD", D(1))
    async with admin.sm() as s:
        p = await create_portfolio(s, name="Portafoglio Crypto", initial_cash=D(1000))
        o = Order(portfolio_id=p.id, symbol="BTC-USD", side=OrderSide.buy,
                  type=OrderType.market, qty=D(1), status=OrderStatus.new)
        s.add(o)
        await s.flush()
        quote = (await admin.qs.get_quotes(["BTC-USD"]))["BTC-USD"]
        assert await execute_fill(s, admin.qs, o.id, quote=quote) is not None
        s.add(Order(portfolio_id=p.id, symbol="BTC-USD", side=OrderSide.sell,
                    type=OrderType.stop, qty=D(1), stop_price=D(90),
                    status=OrderStatus.pending))
        await s.commit()
    r = await admin.client.get("/api/positions/open")  # the viewer reads it
    assert r.status_code == 200, r.text
    body = r.json()
    [row] = [x for x in body["positions"] if x["portfolio_id"] == str(p.id)]
    assert row["symbol"] == "BTC-USD" and row["qty"] == "1" and row["last"] == "100"
    assert row["stop"]["price"] == "90" and row["stop"]["tif"] == "day"
    assert row["protection"] == "scade oggi"  # crypto is always open
    assert D(row["risk_at_stop_eur"]) == 10 and row["target"] is None
    [t] = [x for x in body["portfolios"] if x["portfolio_id"] == str(p.id)]
    limit = D(t["daily_loss_limit_eur"])  # Default 10% of the equity (1000 minus the fees)
    assert D(t["risk_at_stop_eur"]) == 10 and 99 < limit <= 100
    assert D(t["risk_pct_of_limit"]) == (10 / limit * 100).quantize(D("0.01"))
