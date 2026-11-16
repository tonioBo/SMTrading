"""«Strategie»: results per strategy from real sim fills (order + ledger). The opening
order's proposal gives the strategy (the shared attribution helper); exits without a
proposal close the oldest lots; costs are the fills' fees plus the FX fees; a viewer reads
`GET /api/strategies/performance`."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents.service import create_agent
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, OrderSide, OrderType
from smtrading.engine.quotesource import FakeMarketData
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import discard_post_commit
from smtrading.performance.strategies import strategy_performance
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.valuation.service import value_portfolio

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
PASSWORD = "correct-horse-battery"
CENT = D("0.01")


@pytest.fixture
async def env(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))  # Demo
        await s.commit()
    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))

    class NS:
        pass

    ns = NS()
    ns.sm, ns.qs, ns.fresh_db = sm, qs, fresh_db
    yield ns
    await engine.dispose()


async def _portfolio(env, name="Portafoglio Aggressivo", cash=2000) -> uuid.UUID:
    async with env.sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(cash))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _decisions(env, pid, ref="STR-7", props=("PROP-AGG-1",)):
    async with env.sm() as s:
        ceo, _ = await create_agent(s, "CEO")
        pm, _ = await create_agent(s, "Portfolio Manager")
        s.add(Decision(agent_id=ceo.id, kind="strategy", portfolio_id=pid,
                       summary=f"Approvata {ref} rsi_reversion su AAPL",
                       details={"ref": ref, "status": "approvata"}))
        s.add_all([Decision(agent_id=pm.id, kind="proposal", portfolio_id=pid,
                            summary=f"{prop}: buy AAPL", details={
                                "ref": prop, "strategy_id": ref, "outcome": "informativa"})
                   for prop in props])
        await s.commit()


async def _trade(env, pid, side, qty, bid, ask, coid=None) -> uuid.UUID:
    env.qs.set_quote("AAPL", D(bid), D(ask), None)
    async with env.sm() as s:
        order = await place_order(s, env.qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide(side), type=OrderType.market,
            qty=D(qty), client_order_id=coid,
        ), now=OPEN)
        quote = (await env.qs.get_quotes(["AAPL"]))["AAPL"]
        await execute_fill(s, env.qs, order.id, quote=quote)
        discard_post_commit(s)
        await s.commit()
        return order.id


async def _fills(env, order_id) -> list[Fill]:
    async with env.sm() as s:
        return list(await s.scalars(select(Fill).where(Fill.order_id == order_id)))


async def test_empty(env):
    async with env.sm() as s:
        assert await strategy_performance(s, env.qs) == []


async def test_attribution_fifo_costs_and_total(env):
    pid = await _portfolio(env)
    await _decisions(env, pid)
    o1 = await _trade(env, pid, "buy", 2, "199", "200", coid="PROP-AGG-1")  # STR-7
    await _trade(env, pid, "sell", 1, "210", "211")  # no proposal: closes STR-7 FIFO
    o3 = await _trade(env, pid, "buy", 1, "204", "205")  # admin buy: «Senza strategia»
    env.qs.set_quote("AAPL", D("219"), D("221"), None)  # mark 220

    async with env.sm() as s:
        rows = await strategy_performance(s, env.qs)
        v = await value_portfolio(s, env.qs, pid)
    assert [(r.ref, r.portfolio_id) for r in rows] == [("STR-7", pid), (None, pid)]
    strat, none = rows
    assert (strat.name, strat.status) == ("Approvata STR-7 rsi_reversion su AAPL",
                                          "approvata")
    assert (none.name, none.status) == (None, None)
    assert (strat.trades, strat.closed_trades, strat.open_positions) == (1, 0, 1)
    assert strat.win_rate_pct is None  # nothing closed yet
    [f1] = await _fills(env, o1)
    [t] = strat.trade_list
    assert t.order_id == o1 and t.client_order_id == "PROP-AGG-1"
    assert t.qty == 2 and t.open_qty == 1 and t.entry_price == f1.price
    assert t.mark == D(220) and t.closed_at is None
    # USD -> EUR at 0.8
    assert t.realized_pnl_eur == ((t.exit_price - f1.price) * D("0.8")).quantize(CENT)
    assert t.unrealized_pnl_eur == ((D(220) - f1.price) * D("0.8")).quantize(CENT)
    assert strat.costs_eur > 0 and none.costs_eur > 0
    # every euro is accounted for: P/L - costs over all strategies = equity - deposit
    total = sum(r.total_pnl_eur for r in rows)
    assert abs(total - (v.equity_eur - D(2000))) <= D("0.03")
    [n] = none.trade_list
    assert n.order_id == o3 and n.open_qty == 1


async def test_win_rate_on_closed_and_missing_quote(env):
    pid = await _portfolio(env)
    await _decisions(env, pid, props=("PROP-AGG-1", "PROP-AGG-2"))
    await _trade(env, pid, "buy", 1, "199", "200", coid="PROP-AGG-1")
    await _trade(env, pid, "sell", 1, "250", "251")  # a clear win, net of costs
    async with env.sm() as s:
        [r] = await strategy_performance(s, env.qs)
    assert (r.trades, r.closed_trades, r.open_positions) == (1, 1, 0)
    assert r.win_rate_pct == D(100) and r.unrealized_pnl_eur == 0
    assert r.trade_list[0].closed_at is not None and r.trade_list[0].mark is None

    await _trade(env, pid, "buy", 1, "199", "200", coid="PROP-AGG-2")
    env.qs._quotes.pop("AAPL")  # no quote any more: unrealised unknown, never 0
    async with env.sm() as s:
        [r] = await strategy_performance(s, env.qs)
    assert r.trades == 2 and r.unrealized_pnl_eur is None and r.total_pnl_eur is None
    assert r.realized_pnl_eur is not None and r.win_rate_pct == D(100)


async def test_archived_portfolios_are_left_out(env):
    pid = await _portfolio(env)
    await _trade(env, pid, "buy", 1, "199", "200")
    async with env.sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()
        assert await strategy_performance(s, env.qs) == []


async def test_api_viewer_reads(env, redis_url):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime

    pid = await _portfolio(env)
    await _decisions(env, pid)
    await _trade(env, pid, "buy", 1, "199", "200", coid="PROP-AGG-1")
    email = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
    async with env.sm() as s:
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()
    qs = env.qs
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    redis = Redis.from_url(redis_url)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            r = await c.get("/api/strategies/performance")
            assert r.status_code == 200, r.text
            body = r.json()
    finally:
        stop.set()
        await lifespan
        await redis.aclose()
    assert datetime.fromisoformat(body["as_of"])
    [row] = body["strategies"]
    assert row["ref"] == "STR-7" and row["status"] == "approvata"
    assert row["portfolio_name"] == "Portafoglio Aggressivo"
    assert row["trades"] == 1 and row["open_positions"] == 1
    assert isinstance(row["realized_pnl_eur"], str) and row["win_rate_pct"] is None
    [t] = row["trade_list"]
    assert t["symbol"] == "AAPL" and t["direction"] == "long" and t["closed_at"] is None
    assert t["client_order_id"] == "PROP-AGG-1"
