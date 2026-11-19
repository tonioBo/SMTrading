"""«Previsto contro reale»: the metrics stored with the CEO's approval (or the Quant's
registration, or the backtest) against the strategy's live trades since the approval,
from real sim fills; a viewer reads `GET /api/strategies/expectations`."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Decision
from smtrading.agents.service import create_agent
from smtrading.backtest.models import Backtest, BacktestStatus
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.quotesource import FakeMarketData
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import discard_post_commit
from smtrading.performance.expectations import strategy_expectations
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
PASSWORD = "correct-horse-battery"


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
        p = await create_portfolio(s, name="Portafoglio Aggressivo", initial_cash=D(20000))
        ceo, _ = await create_agent(s, "CEO")
        quant, _ = await create_agent(s, "Quant Researcher")
        pm, _ = await create_agent(s, "Portfolio Manager")
        discard_post_commit(s)
        await s.commit()
    qs = FakeMarketData()
    qs.set_fx("EUR", "USD", D("1.25"))

    class NS:
        pass

    ns = NS()
    ns.sm, ns.qs, ns.pid = sm, qs, p.id
    ns.ceo, ns.quant, ns.pm = ceo.id, quant.id, pm.id
    ns.n = 0
    yield ns
    await engine.dispose()


async def _add(env, *decisions):
    async with env.sm() as s:
        s.add_all(decisions)
        await s.commit()


def _strategy(env, ref, status="approvata", **details):
    return Decision(agent_id=env.ceo, kind="strategy", portfolio_id=env.pid,
                    summary=f"{status.capitalize()} {ref}",
                    details={"ref": ref, "status": status, **details})


async def _round_trip(env, ref, win: bool):
    """A proposal of `ref`, a buy at 200 and a sell at 250 (win) or 190 (loss)."""
    env.n += 1
    prop = f"PROP-AGG-{ref}-{env.n}"
    await _add(env, Decision(agent_id=env.pm, kind="proposal", portfolio_id=env.pid,
                             summary=f"{prop}: buy AAPL",
                             details={"ref": prop, "strategy_id": ref}))
    for side, bid, ask, coid in (("buy", "199", "200", prop),
                                 ("sell", "250" if win else "190", "251" if win else "191",
                                  None)):
        env.qs.set_quote("AAPL", D(bid), D(ask), None)
        async with env.sm() as s:
            await place_order(s, env.qs, PlaceOrderRequest(
                portfolio_id=env.pid, symbol="AAPL", side=OrderSide(side),
                type=OrderType.market, qty=D(1), client_order_id=coid), now=OPEN)
            discard_post_commit(s)
            await s.commit()


async def test_expected_against_live(env):
    async with env.sm() as s:
        bt = Backtest(strategy="rsi_reversion", symbols=["AAPL"], timeframe="1d",
                      start=datetime(2025, 11, 1, tzinfo=UTC),
                      end=datetime(2026, 11, 1, tzinfo=UTC), initial_cash=D(1000),
                      params={}, status=BacktestStatus.completed,
                      result={"metrics": {"win_rate_pct": "60", "max_drawdown_pct": "8",
                                          "sharpe": "1.1", "trades": 12,
                                          "total_return_pct": "14"},
                              "trades": [{"pnl": "10", "entry_price": "100", "qty": "1"},
                                         {"pnl": "-2", "entry_price": "100", "qty": "1"}]})
        s.add(bt)
        await s.commit()

    # STR-7: a trade before the approval does not count
    await _round_trip(env, "STR-7", win=True)
    await _add(env, _strategy(env, "STR-7", backtest_id=str(bt.id), metrics={
        "win_rate_pct": "50", "max_drawdown_pct": "-10", "trades": 12}))
    for win in (True, False, False, False, False, False):
        await _round_trip(env, "STR-7", win)
    # STR-8: only the Quant's registration has metrics, 2 trades
    await _add(env, Decision(agent_id=env.quant, kind="other", portfolio_id=env.pid,
                             summary="Backtest STR-8", details={
                                 "ref": "STR-8", "strategy": "sma_cross",
                                 "from_date": "2023-11-01", "to_date": "2026-11-01",
                                 "metrics": {"win_rate_pct": "45", "trades": 36,
                                             "max_drawdown_pct": "-11.2"}}),
               _strategy(env, "STR-8"))
    await _round_trip(env, "STR-8", True)
    await _round_trip(env, "STR-8", False)
    # STR-9: no metrics anywhere
    await _add(env, _strategy(env, "STR-9"))
    # STR-10: five wins, then suspended (still compared); a modificata merges its metrics
    await _add(env, _strategy(env, "STR-10", metrics={"win_rate_pct": "55"}),
               _strategy(env, "STR-10", status="modificata", metrics={
                   "win_rate_pct": "55", "max_drawdown_pct": "-6"}))
    for _ in range(5):
        await _round_trip(env, "STR-10", True)
    await _add(env, _strategy(env, "STR-10", status="sospesa"))
    # STR-11: respinta, never approved: not listed
    await _add(env, _strategy(env, "STR-11", status="respinta"))

    async with env.sm() as s:
        rows = await strategy_expectations(s, env.qs)
    assert [r.ref for r in rows] == ["STR-7", "STR-8", "STR-9", "STR-10"]
    s7, s8, s9, s10 = rows

    e = s7.expected
    assert e.source == "approvazione" and e.backtest_id == bt.id
    assert (e.win_rate_pct, e.max_drawdown_pct, e.trades) == (D(50), D(10), 12)
    assert (e.sharpe, e.total_return_pct) == (D("1.1"), D(14))  # from the backtest
    assert e.avg_trade_pct == D(4)  # (10% - 2%) / 2
    assert (e.from_date.isoformat(), e.to_date.isoformat()) == ("2025-11-01", "2026-11-01")
    assert e.trades_per_month == D("1.00")  # 12 trades in a year
    live = s7.live
    assert (live.trades, live.closed_trades, live.wins) == (6, 6, 1)
    assert live.win_rate_pct == D("16.7") and live.avg_trade_pct < 0
    assert live.max_drawdown_pct > 0 and live.max_drawdown_eur > 0 and live.pnl_eur < 0
    assert s7.verdict == "sotto le attese"
    assert any(r.startswith("vincenti 16,7% contro 50,0%") for r in s7.reasons)

    assert s8.expected.source == "quant" and s8.expected.win_rate_pct == D(45)
    assert s8.expected.trades_per_month == D("1.00")  # 36 trades in 3 years
    assert s8.live.closed_trades == 2 and s8.verdict == "dati insufficienti"
    assert s8.reasons == ["2 operazioni chiuse: ne servono almeno 5"]

    assert s9.expected.source is None and s9.verdict == "dati insufficienti"
    assert s9.live.trades == 0 and s9.live.win_rate_pct is None

    assert s10.status == "sospesa" and s10.expected.max_drawdown_pct == D(6)
    assert s10.live.wins == 5 and s10.live.max_drawdown_pct == 0
    assert s10.verdict == "in linea" and s10.reasons == []


async def test_api_viewer_reads(env, redis_url):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.admin.routes_strategies import EXPECTATIONS_KEY
    from smtrading.api.main import create_app
    from smtrading.mcp.runtime import McpRuntime

    await _add(env, _strategy(env, "STR-7", metrics={"win_rate_pct": "50"}))
    await _round_trip(env, "STR-7", True)
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
    await redis.delete(EXPECTATIONS_KEY)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            r = await c.get("/api/strategies/expectations")
            assert r.status_code == 200, r.text
            body = r.json()
        await redis.delete(EXPECTATIONS_KEY)
    finally:
        stop.set()
        await lifespan
        await redis.aclose()
    assert body["min_trades"] == 5
    [row] = body["strategies"]
    assert row["ref"] == "STR-7" and row["verdict"] == "dati insufficienti"
    assert row["expected"]["win_rate_pct"] == "50"
    assert row["live"]["trades"] == 1 and row["live"]["win_rate_pct"] == "100"
