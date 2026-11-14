"""MCP end to end on the live fixture server, plus the Paperclip docs staying in sync."""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.db import make_engine
from smtrading.mcp.server import build_mcp
from smtrading.risk.models import Notification
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
DOC = Path(__file__).resolve().parents[3] / "docs" / "paperclip-mcp.md"

ALL_TOOLS = {
    # portfolio
    "list_portfolios", "get_portfolio", "get_positions", "get_transactions",
    "get_performance", "get_risk_limits",
    # orders
    "preview_order", "place_order", "place_bracket_order", "place_oco_order", "modify_order",
    "cancel_order", "cancel_all_orders", "get_order", "list_orders", "close_position",
    "close_all_positions",
    # market
    "search_instruments", "get_instrument", "get_quote", "get_bars", "get_market_status",
    "get_indicators", "get_fx_rate", "get_news", "get_fundamentals", "get_earnings_calendar",
    "get_dividends", "screen",
    # alerts
    "get_triggered_alerts",
    # watchlists and price alerts
    "list_watchlists", "create_watchlist", "add_to_watchlist", "remove_from_watchlist",
    "delete_watchlist", "create_alert", "list_alerts", "delete_alert",
    # backtests
    "run_backtest", "get_backtest", "list_backtests",
    # decisions, activity feed, Telegram
    "log_decision", "get_decisions", "get_activity_feed", "send_telegram",
}


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        await s.commit()
    await engine.dispose()
    mcp_server.qs.set_fx("EUR", "USD", D("1.25"))
    mcp_server.qs.set_quote(SYM, D(50000), D(50000), D(50000))

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    return call


async def test_list_tools_is_the_full_fixed_set(mcp_server, mcp_list_tools):
    names = set(await mcp_list_tools(mcp_server.url, mcp_server.token))
    assert ALL_TOOLS <= names
    assert names == ALL_TOOLS


async def test_full_flow(mcp_server, env):
    call = env
    err, portfolios = await call("list_portfolios")
    assert not err
    [demo] = [p for p in portfolios if p["name"] == "Demo"]
    pid = demo["id"]

    err, quotes = await call("get_quote", {"symbols": [SYM]})
    assert not err and D(quotes[SYM]["last"]) == 50000

    order = {"portfolio_id": pid, "symbol": SYM, "side": "buy", "type": "market", "qty": "0.1"}
    err, preview = await call("preview_order", order)
    assert not err, preview
    err, placed = await call("place_order", order)
    assert not err and placed["status"] == "filled", placed

    err, positions = await call("get_positions", {"portfolio_id": pid})
    assert not err and D(positions[0]["qty"]) == D("0.1")
    err, txs = await call("get_transactions", {"portfolio_id": pid})
    assert not err and txs

    err, closed = await call("close_position", {"portfolio_id": pid, "symbol": SYM})
    assert not err and closed["status"] == "filled"
    err, positions = await call("get_positions", {"portfolio_id": pid})
    assert not err and positions == []

    err, alerts = await call("get_triggered_alerts")
    assert not err and isinstance(alerts["alerts"], list)


async def test_triggered_alerts_scope_order_and_since(mcp_server, env):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.add_all([
            Notification(portfolio_id=mcp_server.demo_id, kind="margin_warning", payload={"n": 1}),
            Notification(agent_id=str(mcp_server.agent_id), kind="alert", payload={"n": 2}),
            Notification(portfolio_id=None, agent_id="someone-else", kind="alert",
                         payload={"n": 3}),
        ])
        await s.commit()
    await engine.dispose()

    err, page = await env("get_triggered_alerts")
    assert not err
    alerts = page["alerts"]
    assert [a["payload"]["n"] for a in alerts] == [2, 1]  # newest first, foreign one excluded
    assert set(alerts[0]) == {"id", "portfolio_id", "kind", "payload", "created_at"}
    assert page["next_after_id"] == alerts[0]["id"]

    err, none = await env("get_triggered_alerts", {"since": alerts[0]["created_at"]})
    assert not err and none == {"alerts": [], "next_after_id": None}
    err, limited = await env("get_triggered_alerts", {"limit": 1})
    assert not err and len(limited["alerts"]) == 1
    err, bad = await env("get_triggered_alerts", {"since": "yesterday"})
    assert err and bad["code"] == "INVALID_ORDER"
    err, bad = await env("get_triggered_alerts", {"limit": 501})
    assert err and bad["code"] == "INVALID_ORDER"
    err, bad = await env("get_triggered_alerts", {"after_id": 10**9})
    assert err and bad["details"]["field"] == "after_id"


async def test_triggered_alerts_polling_cursor(mcp_server, env):
    engine = make_engine(mcp_server.db_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            # one transaction: identical created_at, ordered by id
            s.add_all([Notification(portfolio_id=mcp_server.demo_id, kind="alert",
                                    payload={"n": i}) for i in range(3)])
            await s.commit()
        err, first = await env("get_triggered_alerts", {"limit": 1})
        assert not err and [a["payload"]["n"] for a in first["alerts"]] == [2]
        async with AsyncSession(engine, expire_on_commit=False) as s:
            s.add(Notification(portfolio_id=mcp_server.demo_id, kind="alert", payload={"n": -1}))
            await s.commit()

        err, all_ = await env("get_triggered_alerts", {"since": "2000-01-01T00:00:00Z"})
        assert not err
        ns = [a["payload"]["n"] for a in all_["alerts"]]
        assert ns == [0, 1, 2, -1]  # ascending with a cursor
        ids = [a["id"] for a in all_["alerts"]]

        seen, after = [], ids[0]
        while True:
            err, page = await env("get_triggered_alerts", {"after_id": after, "limit": 2})
            assert not err
            seen += [a["id"] for a in page["alerts"]]
            if not page["alerts"]:
                assert page["next_after_id"] == after
                break
            after = page["next_after_id"]
        assert seen == ids[1:]
    finally:
        await engine.dispose()


async def test_docs_list_every_registered_tool():
    text = DOC.read_text()
    documented = set(re.findall(r"^- `([a-z_]+)`", text, re.MULTILINE))
    registered = {t.name for t in await build_mcp().list_tools()}
    assert registered == documented
