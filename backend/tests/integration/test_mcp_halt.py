"""MCP set_trading_halt / get_trading_halt on the live fixture server."""

from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Activity
from smtrading.agents import service as agents
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.common.db import make_engine
from smtrading.portfolios.service import create_portfolio
from smtrading.risk import halts
from smtrading.risk.models import TradingHalt

D = Decimal


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)

    async def call(tool, args=None, token=None):
        return await mcp_call(mcp_server.url, token or mcp_server.token, tool, args or {})

    async def db():
        return AsyncSession(engine, expire_on_commit=False)

    yield {"call": call, "server": mcp_server, "db": db}
    await engine.dispose()


async def _grant(env, on=True):
    async with await env["db"]() as s:
        await s.execute(update(Agent).where(Agent.id == env["server"].agent_id)
                        .values(can_halt=on))
        await s.commit()


async def _rows(env):
    async with await env["db"]() as s:
        return list(await s.scalars(select(TradingHalt).order_by(TradingHalt.id)))


async def test_set_needs_halt_permission(env):
    err, res = await env["call"]("set_trading_halt", {"active": True, "reason": "x"})
    assert err and res["code"] == "FORBIDDEN" and res["details"] == {"needed": "halt"}
    assert await _rows(env) == []


async def test_global_halt_set_get_clear(env):
    call, server = env["call"], env["server"]
    await _grant(env)
    err, res = await call("set_trading_halt", {"scope": "global", "active": "true",
                                               "reason": "Drawdown oltre il 5%"})
    assert not err, res
    assert res["changed"] is True and res["active"] is True
    halt = res["halt"]
    assert halt["scope"] == "global" and halt["portfolio_id"] is None
    assert halt["reason"] == "Drawdown oltre il 5%" and halt["set_by"] == "test-agent"
    assert halt["since"]

    err, again = await call("set_trading_halt", {"active": True, "reason": "Drawdown oltre il 5%"})
    assert not err and again["changed"] is False and again["halt"]["id"] == halt["id"]

    err, got = await call("get_trading_halt")
    assert not err and got["halted"] is True and [h["id"] for h in got["halts"]] == [halt["id"]]
    err, got = await call("get_trading_halt", {"portfolio_id": str(server.demo_id)})
    assert not err and got["halted"] is True

    # the agent's own opening order is refused
    server.qs.set_quote("BTC-USD", D(50000), D(50000), D(50000))
    server.qs.set_fx("EUR", "USD", D("1.25"))
    err, res = await call("place_order", {"portfolio_id": str(server.demo_id),
                                          "symbol": "BTC-USD", "side": "buy",
                                          "type": "market", "qty": "0.01"})
    assert err and res["code"] == "TRADING_HALTED"
    assert res["message"] == "Aperture sospese dal CEO: Drawdown oltre il 5%"

    err, res = await call("set_trading_halt", {"active": "false"})
    assert not err and res["changed"] is True and res["active"] is False
    assert res["halt"]["cleared_by"] == "test-agent"
    err, res = await call("set_trading_halt", {"active": False})
    assert not err and res == {"active": False, "changed": False, "halt": None}
    err, got = await call("get_trading_halt")
    assert not err and got == {"halted": False, "halts": []}

    async with await env["db"]() as s:
        acts = list(await s.scalars(select(Activity).where(Activity.type.like("halt.%"))
                                    .order_by(Activity.id)))
        audit = list(await s.scalars(select(McpAuditLog.params).where(
            McpAuditLog.method == "set_trading_halt").order_by(McpAuditLog.id)))
    assert [(a.type, a.level, a.telegram, a.agent_id) for a in acts] == [
        ("halt.set", "critical", "pending", server.agent_id),
        ("halt.cleared", "critical", "pending", server.agent_id),
    ]
    assert audit[0] == {"scope": "global", "portfolio_id": None, "active": "true",
                        "reason": "Drawdown oltre il 5%"}


async def test_portfolio_scope_needs_read_on_it(env):
    call, server = env["call"], env["server"]
    await _grant(env)
    async with await env["db"]() as s:
        other = await create_portfolio(s, name="Other", initial_cash=D(1000))
        await s.commit()
    err, res = await call("set_trading_halt", {"scope": "portfolio",
                                               "portfolio_id": str(other.id),
                                               "active": True, "reason": "x"})
    assert err and res["code"] == "FORBIDDEN"
    err, res = await call("get_trading_halt", {"portfolio_id": str(other.id)})
    assert err and res["code"] == "FORBIDDEN"

    err, res = await call("set_trading_halt", {"scope": "portfolio",
                                               "portfolio_id": str(server.demo_id),
                                               "active": True, "reason": "Solo Demo"})
    assert not err and res["halt"]["portfolio"] == "Demo"

    # a halt on a portfolio the agent does not see stays hidden from it
    async with await env["db"]() as s:
        await halts.set_halt(s, scope="portfolio", portfolio_id=other.id, reason="altro",
                             by="admin:admin@example.com")
        await s.commit()
    err, got = await call("get_trading_halt")
    assert not err and [h["portfolio"] for h in got["halts"]] == ["Demo"]


async def test_admin_author_shown_as_admin(env):
    async with await env["db"]() as s:
        await halts.set_halt(s, scope="global", portfolio_id=None, reason="manuale",
                             by="admin:boss@example.com")
        await s.commit()
    err, got = await env["call"]("get_trading_halt")
    assert not err and got["halts"][0]["set_by"] == "admin"


@pytest.mark.parametrize("args,field", [
    ({"active": True}, "reason"),
    ({"active": True, "reason": "x" * 501}, "reason"),
    ({"active": "maybe", "reason": "x"}, "active"),
    ({"scope": "all", "active": True, "reason": "x"}, "scope"),
    ({"scope": "portfolio", "active": True, "reason": "x"}, "portfolio_id"),
    ({"scope": "portfolio", "portfolio_id": "nope", "active": True, "reason": "x"},
     "portfolio_id"),
])
async def test_invalid_arguments(env, args, field):
    await _grant(env)
    err, res = await env["call"]("set_trading_halt", args)
    assert err and res["code"] == "INVALID_ORDER" and res["details"]["field"] == field


async def test_permission_read_at_call_time(env):
    await _grant(env)
    err, _ = await env["call"]("set_trading_halt", {"active": True, "reason": "x"})
    assert not err
    await _grant(env, False)
    err, res = await env["call"]("set_trading_halt", {"active": False})
    assert err and res["code"] == "FORBIDDEN"


async def test_can_halt_defaults_false_and_toggles(env):
    async with await env["db"]() as s:
        agent = await s.get(Agent, env["server"].agent_id)
        assert agent.can_halt is False
        assert (await agents.set_agent_can_halt(s, agent.id, True)).can_halt is True
        await s.commit()
