"""MCP watchlist and alert tools and backtest tools on the live fixture server."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.backtest.runner import BacktestRunner
from smtrading.common.db import make_engine
from smtrading.marketdata.streaming import TRACKED_KEY
from smtrading.marketdata.types import Bar, Quote
from smtrading.seed import run_seed
from smtrading.watch.evaluator import AlertEvaluator

D = Decimal
SYM = "BTC-USD"

WATCH_TOOLS = {
    "list_watchlists", "create_watchlist", "add_to_watchlist", "remove_from_watchlist",
    "delete_watchlist", "create_alert", "list_alerts", "delete_alert",
}


@pytest.fixture
async def env(mcp_server, mcp_call, redis_url):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        await s.commit()
    await engine.dispose()
    mcp_server.qs.set_quote(SYM, D(49999), D(50001), D(50000))
    redis = Redis.from_url(redis_url)
    await redis.delete(f"{TRACKED_KEY}:watchlists", f"{TRACKED_KEY}:alerts")

    async def call(tool, args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token, tool, args or {})

    yield {"call": call, "redis": redis, "server": mcp_server}
    await redis.aclose()


async def _tracked(redis, source):
    return {m.decode() for m in await redis.smembers(f"{TRACKED_KEY}:{source}")}


async def test_watch_tools_listed(mcp_server, mcp_list_tools):
    assert WATCH_TOOLS <= set(await mcp_list_tools(mcp_server.url, mcp_server.token))


async def test_watchlist_flow(env):
    call = env["call"]
    err, wl = await call("create_watchlist", {"name": "crypto"})
    assert not err and wl["name"] == "crypto" and wl["symbols"] == []
    err, wl2 = await call("add_to_watchlist",
                          {"watchlist_id": wl["id"], "symbols": ["btc-usd", "AAPL"]})
    assert not err and wl2["symbols"] == ["AAPL", "BTC-USD"]
    assert await _tracked(env["redis"], "watchlists") == {"AAPL", "BTC-USD"}

    err, wl3 = await call("remove_from_watchlist",
                          {"watchlist_id": wl["id"], "symbols": ["AAPL"]})
    assert not err and wl3["symbols"] == ["BTC-USD"]
    assert await _tracked(env["redis"], "watchlists") == {"BTC-USD"}

    err, lists = await call("list_watchlists")
    assert not err and [w["id"] for w in lists] == [wl["id"]]
    assert set(lists[0]) == {"id", "name", "symbols", "created_at"}

    err, out = await call("delete_watchlist", {"watchlist_id": wl["id"]})
    assert not err and out == {"id": wl["id"], "deleted": True}
    assert await _tracked(env["redis"], "watchlists") == set()

    err, bad = await call("add_to_watchlist", {"watchlist_id": "nope", "symbols": ["AAPL"]})
    assert err and bad["code"] == "INVALID_ORDER" and bad["details"]["field"] == "watchlist_id"
    err, bad = await call("add_to_watchlist", {"watchlist_id": wl["id"], "symbols": ["AAPL"]})
    assert err and bad["code"] == "NOT_FOUND"
    err, bad = await call("create_watchlist", {"name": ""})
    assert err and bad["code"] == "INVALID_ORDER"


async def test_alert_triggers_and_shows_in_get_triggered_alerts(env):
    call, server = env["call"], env["server"]
    err, alert = await call("create_alert", {
        "symbol": SYM, "condition": "price_above", "value": "51000", "note": "breakout"})
    assert not err and alert["status"] == "active" and alert["value"] == "51000"
    assert set(alert) == {"id", "symbol", "condition", "value", "ref_price", "status",
                          "note", "created_at", "triggered_at", "triggered_price"}
    assert await _tracked(env["redis"], "alerts") == {SYM}
    err, first = await call("get_triggered_alerts")
    cursor = first["next_after_id"]

    engine = make_engine(server.db_url)
    ev = AlertEvaluator(async_sessionmaker(engine, expire_on_commit=False), env["redis"],
                        server.qs)
    await ev.sweep()
    tick = Quote(symbol=SYM, bid=D(51000), ask=D(51002), last=D(51001), ts=datetime.now(UTC),
                 provider="fake")
    assert await ev.on_tick(SYM, tick) == 1
    await engine.dispose()

    args = {"after_id": cursor} if cursor is not None else {}
    err, page = await call("get_triggered_alerts", args)
    assert not err
    [n] = [a for a in page["alerts"] if a["kind"] == "price_alert"]
    assert n["payload"] == {"alert_id": alert["id"], "symbol": SYM, "condition": "price_above",
                            "value": "51000", "price": "51001"}

    err, rows = await call("list_alerts", {"status": "triggered"})
    assert not err and [r["id"] for r in rows] == [alert["id"]]
    assert rows[0]["triggered_price"] == "51001"
    err, rows = await call("list_alerts", {"status": "active"})
    assert not err and rows == []


async def test_alert_errors_and_foreign_agent(env):
    call, server = env["call"], env["server"]
    err, alert = await call("create_alert", {
        "symbol": SYM, "condition": "change_pct_below", "value": -5})
    assert not err and alert["ref_price"] == "50000"

    err, bad = await call("create_alert", {"symbol": SYM, "condition": "up", "value": "1"})
    assert err and bad["code"] == "INVALID_ORDER" and bad["details"]["field"] == "condition"
    err, bad = await call("create_alert",
                          {"symbol": "ETH-USD", "condition": "change_pct_above", "value": "5"})
    assert err and bad["code"] == "INVALID_ORDER" and bad["message"] == "quota non disponibile"
    err, bad = await call("list_alerts", {"status": "deleted"})
    assert err and bad["code"] == "INVALID_ORDER"

    engine = make_engine(server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        _, other_token = await create_agent(s, "other-agent")
        await s.commit()
    await engine.dispose()
    err, bad = await call("delete_alert", {"alert_id": alert["id"]}, token=other_token)
    assert err and bad["code"] == "FORBIDDEN"
    err, rows = await call("list_alerts", token=other_token)
    assert not err and rows == []

    err, gone = await call("delete_alert", {"alert_id": alert["id"]})
    assert not err and gone["status"] == "deleted"
    assert await _tracked(env["redis"], "alerts") == set()
    err, bad = await call("delete_alert", {"alert_id": alert["id"]})
    assert err and bad["code"] == "NOT_FOUND"


BT_ARGS = {"strategy": "buy_and_hold", "symbols": [SYM], "from_date": "2024-01-01",
           "to_date": "2024-03-01", "initial_cash": "10000"}


async def test_backtest_flow_and_isolation(env):
    call, server = env["call"], env["server"]
    server.qs.set_bars(SYM, [
        Bar(ts=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i), open=D(100 + i),
            high=D(101 + i), low=D(99 + i), close=D(100 + i), volume=D(1))
        for i in range(60)])
    err, out = await call("run_backtest", BT_ARGS)
    assert not err and out["status"] == "queued"
    bid = out["backtest_id"]
    err, bt = await call("get_backtest", {"backtest_id": bid})
    assert not err and bt["status"] == "queued" and bt["result"] is None

    engine = make_engine(server.db_url)
    await BacktestRunner(async_sessionmaker(engine, expire_on_commit=False),
                         server.qs).run_once()
    await engine.dispose()
    err, bt = await call("get_backtest", {"backtest_id": bid})
    assert not err and bt["status"] == "completed"
    assert set(bt["result"]["metrics"]) == {
        "total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "win_rate_pct",
        "trades", "exposure_pct"}
    err, rows = await call("list_backtests", {"status": "completed"})
    assert not err and [r["id"] for r in rows] == [bid]
    assert "result" not in rows[0] and rows[0]["metrics"] == bt["result"]["metrics"]

    engine = make_engine(server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        _, other = await create_agent(s, "other-bt")
        await s.commit()
    await engine.dispose()
    err, bad = await call("get_backtest", {"backtest_id": bid}, token=other)
    assert err and bad["code"] == "FORBIDDEN"
    err, rows = await call("list_backtests", token=other)
    assert not err and rows == []


async def test_backtest_tool_errors(env):
    call = env["call"]
    err, bad = await call("run_backtest", {**BT_ARGS, "strategy": "magic"})
    assert err and bad["code"] == "INVALID_ORDER"
    err, bad = await call("run_backtest", {**BT_ARGS, "from_date": "yesterday"})
    assert err and bad["code"] == "INVALID_ORDER" and bad["details"]["field"] == "from_date"
    err, bad = await call("run_backtest", {**BT_ARGS, "to_date": "2031-01-01"})
    assert err and bad["code"] == "RISK_LIMIT_EXCEEDED"
    err, bad = await call("run_backtest", {**BT_ARGS, "portfolio_id": "5f0e7c1e-0000-4000-8000-"
                                           "000000000000"})
    assert err and bad["code"] == "FORBIDDEN"
    err, bad = await call("get_backtest", {"backtest_id": "x"})
    assert err and bad["details"]["field"] == "backtest_id"
    for _ in range(2):
        err, _ = await call("run_backtest", BT_ARGS)
        assert not err
    err, bad = await call("run_backtest", BT_ARGS)
    assert err and bad["code"] == "RATE_LIMITED"
