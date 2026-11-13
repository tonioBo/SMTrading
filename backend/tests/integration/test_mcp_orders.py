"""MCP order tools on the live fixture server. BTC-USD (market CRYPTO) is always open."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agents import service as agents
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"


class _Db:
    def __init__(self, url: str) -> None:
        self.engine = make_engine(url)

    async def __aenter__(self) -> AsyncSession:
        self.session = AsyncSession(self.engine, expire_on_commit=False)
        return self.session

    async def __aexit__(self, *exc) -> None:
        await self.session.close()
        await self.engine.dispose()


@pytest.fixture
async def env(mcp_server, mcp_call):
    """Seeded instruments/fees; BTC-USD at 50000 USD, EURUSD 1.25; Other: unassigned."""
    qs = mcp_server.qs
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote(SYM, D(50000), D(50000), D(50000))
    async with _Db(mcp_server.db_url) as s:
        await run_seed(s)
        other = await create_portfolio(s, name="Other", initial_cash=D(5000))
        await s.commit()

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    return {"call": call, "pid": str(mcp_server.demo_id), "other_id": other.id}


def _buy(env, **kw):
    args = {"portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market",
            "qty": "0.1"}
    args.update(kw)
    return env["call"]("place_order", args)


async def test_tools_listed(mcp_server, mcp_list_tools):
    names = set(await mcp_list_tools(mcp_server.url, mcp_server.token))
    assert {
        "preview_order", "place_order", "place_bracket_order", "place_oco_order",
        "modify_order", "cancel_order", "cancel_all_orders", "get_order", "list_orders",
        "close_position", "close_all_positions",
    } <= names


async def test_market_buy_fills_and_updates_cash(mcp_server, env):
    is_error, o = await _buy(env)
    assert not is_error, o
    assert o["status"] == "filled"
    assert D(o["filled_qty"]) == D("0.1") and D(o["avg_fill_price"]) == D(50000)
    assert o["source"] == "agent" and o["agent_id"] == str(mcp_server.agent_id)
    assert o["reduce_only"] is False

    _, full = await env["call"]("get_order", {"order_id": o["id"]})
    [fill] = full["fills"]
    assert D(fill["qty"]) == D("0.1") and D(fill["price"]) == D(50000)

    _, p = await env["call"]("get_portfolio", {"portfolio_id": env["pid"]})
    # 5000 USD = 4000 EUR, plus a (positive) fee
    assert D(95000) < D(p["cash_eur"]) < D(96000)
    [pos] = p["positions"]
    assert pos["symbol"] == SYM and D(pos["qty"]) == D("0.1")


async def test_preview_is_not_an_error(env):
    args = {"portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market"}
    is_error, ok = await env["call"]("preview_order", {**args, "qty": "0.1"})
    assert not is_error and ok["ok"] is True and D(ok["estimated_price"]) == D(50000)
    is_error, bad = await env["call"]("preview_order", {**args, "qty": "1000"})
    assert not is_error
    assert bad["ok"] is False and bad["code"] == "INSUFFICIENT_BUYING_POWER"
    _, orders = await env["call"]("list_orders", {"portfolio_id": env["pid"]})
    assert orders == []


async def test_limit_pending_modify_then_cancel(env):
    is_error, o = await _buy(env, type="limit", limit_price="40000")
    assert not is_error, o
    assert o["status"] == "pending"
    is_error, m = await env["call"]("modify_order", {"order_id": o["id"], "limit_price": 41000})
    assert not is_error, m
    assert D(m["limit_price"]) == D(41000)
    is_error, c = await env["call"]("cancel_order", {"order_id": o["id"]})
    assert not is_error, c
    assert c["status"] == "canceled"
    _, g = await env["call"]("get_order", {"order_id": o["id"]})
    assert g["status"] == "canceled" and g["fills"] == []


async def test_rejected_order_is_persisted(env):
    is_error, err = await _buy(env, qty="1000")
    assert is_error
    assert err["code"] == "INSUFFICIENT_BUYING_POWER"
    is_error, orders = await env["call"](
        "list_orders", {"portfolio_id": env["pid"], "status": ["rejected"]}
    )
    assert not is_error, orders
    [o] = orders
    assert o["status"] == "rejected" and o["reject_code"] == "INSUFFICIENT_BUYING_POWER"
    _, pending = await env["call"](
        "list_orders", {"portfolio_id": env["pid"], "status": "pending"}
    )
    assert pending == []


async def test_error_after_an_order_was_stored_rolls_back(env, monkeypatch):
    """commit_on_error keeps persisted rejections only: an order already written as
    pending is rolled back when another error (here PROVIDER_UNAVAILABLE) follows."""
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.engine import service as engine

    real = engine.place_order

    async def flaky(session, qs, req, **kw):
        order = await real(session, qs, req, **kw)
        assert order.status == OrderStatus.pending
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "provider down")

    monkeypatch.setattr(engine, "place_order", flaky)
    is_error, err = await _buy(env, type="limit", limit_price="40000")
    assert is_error and err["code"] == "PROVIDER_UNAVAILABLE"
    monkeypatch.setattr(engine, "place_order", real)
    _, orders = await env["call"]("list_orders", {"portfolio_id": env["pid"]})
    assert orders == []


async def test_client_order_id_is_idempotent(env):
    _, first = await _buy(env, type="limit", limit_price="40000", client_order_id="c-1")
    is_error, again = await _buy(env, type="limit", limit_price="40000", client_order_id="c-1")
    assert not is_error, again
    assert again["id"] == first["id"] and again["client_order_id"] == "c-1"
    _, orders = await env["call"]("list_orders", {"portfolio_id": env["pid"]})
    assert len(orders) == 1


async def test_rejected_client_order_id_replay_reraises(env):
    _, err = await _buy(env, qty="1000", client_order_id="big")
    is_error, again = await _buy(env, qty="1000", client_order_id="big")
    assert is_error and again["code"] == err["code"] == "INSUFFICIENT_BUYING_POWER"


async def test_read_permission_cannot_trade(mcp_server, env):
    async with _Db(mcp_server.db_url) as s:
        await agents.assign_portfolio(s, mcp_server.agent_id, env["other_id"], "read")
        await s.commit()
    pid = str(env["other_id"])
    other = {"portfolio_id": pid, "symbol": SYM, "side": "buy", "type": "market",
             "qty": "0.01"}
    leg = {"type": "limit", "limit_price": "40000"}
    for tool, args in (
        ("place_order", other),
        ("place_bracket_order", {"portfolio_id": pid, "symbol": SYM, "side": "buy",
                                 "qty": "0.01", "take_profit": "60000",
                                 "stop_loss": "40000"}),
        ("place_oco_order", {"portfolio_id": pid, "symbol": SYM, "side": "buy",
                             "qty": "0.01", "a": leg,
                             "b": {"type": "stop", "stop_price": "60000"}}),
        ("close_position", {"portfolio_id": pid, "symbol": SYM}),
        ("close_all_positions", {"portfolio_id": pid}),
        ("cancel_all_orders", {"portfolio_id": pid}),
    ):
        is_error, err = await env["call"](tool, args)
        assert is_error and err["code"] == "FORBIDDEN", (tool, err)
    # read is enough for preview and list
    is_error, preview = await env["call"]("preview_order", other)
    assert not is_error and preview["ok"] is True
    is_error, _ = await env["call"]("list_orders", {"portfolio_id": str(env["other_id"])})
    assert not is_error


async def test_order_of_another_portfolio_is_forbidden(mcp_server, env):
    async with _Db(mcp_server.db_url) as s:
        o = Order(
            portfolio_id=env["other_id"], symbol=SYM, side=OrderSide.buy,
            type=OrderType.limit, qty=D("0.01"), limit_price=D(40000),
            status=OrderStatus.pending, filled_qty=D(0),
        )
        s.add(o)
        await s.commit()
    for tool, extra in (("cancel_order", {}), ("get_order", {}),
                        ("modify_order", {"limit_price": "41000"})):
        is_error, err = await env["call"](tool, {"order_id": str(o.id), **extra})
        assert is_error and err["code"] == "FORBIDDEN", (tool, err)
    is_error, err = await env["call"]("cancel_order", {"order_id": str(uuid.uuid4())})
    assert is_error and err["code"] == "NOT_FOUND"


@pytest.mark.parametrize("args,field", [
    ({"qty": "abc"}, "qty"),
    ({"side": "up"}, "side"),
    ({"type": "iceberg"}, "type"),
    ({"tif": "forever"}, "tif"),
    ({"portfolio_id": "nope"}, "portfolio_id"),
])
async def test_invalid_arguments(env, args, field):
    is_error, err = await _buy(env, **args)
    assert is_error
    assert err["code"] == "INVALID_ORDER" and err["details"]["field"] == field


async def test_bracket_order(env):
    is_error, orders = await env["call"]("place_bracket_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "qty": "0.1",
        "take_profit": "60000", "stop_loss": "45000",
    })
    assert not is_error, orders
    entry, tp, sl = orders
    assert entry["role"] == "entry" and entry["status"] == "filled"
    assert tp["role"] == "take_profit" and D(tp["limit_price"]) == D(60000)
    assert sl["role"] == "stop_loss" and D(sl["stop_price"]) == D(45000)
    assert tp["parent_id"] == sl["parent_id"] == entry["id"]
    assert tp["oco_group_id"] == sl["oco_group_id"] is not None
    assert tp["reduce_only"] is True and sl["reduce_only"] is True


async def test_bracket_requires_prices(env):
    is_error, err = await env["call"]("place_bracket_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "qty": "0.1",
        "take_profit": "60000",
    })
    assert is_error and err["code"] == "INVALID_ORDER"
    assert err["details"]["field"] == "stop_loss"


async def test_oco_order(env):
    is_error, orders = await env["call"]("place_oco_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "qty": "0.1",
        "a": {"type": "limit", "limit_price": "40000"},
        "b": {"type": "stop", "stop_price": 60000},
    })
    assert not is_error, orders
    a, b = orders
    assert a["type"] == "limit" and b["type"] == "stop"
    assert a["status"] == b["status"] == "pending"
    assert a["oco_group_id"] == b["oco_group_id"] is not None

    is_error, r = await env["call"]("cancel_all_orders", {"portfolio_id": env["pid"]})
    assert not is_error and r == {"canceled": 2}


async def test_oco_leg_unknown_key_is_invalid(env):
    is_error, err = await env["call"]("place_oco_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "qty": "0.1",
        "a": {"type": "limit", "limit_price": "40000", "colour": "red"},
        "b": {"type": "stop", "stop_price": "60000"},
    })
    assert is_error and err["code"] == "INVALID_ORDER" and err["details"]["field"] == "a"


async def test_close_position(env):
    await _buy(env)
    is_error, c = await env["call"](
        "close_position", {"portfolio_id": env["pid"], "symbol": SYM, "qty": "0.04"}
    )
    assert not is_error, c
    assert c["side"] == "sell" and c["status"] == "filled" and c["reduce_only"] is True
    _, positions = await env["call"]("get_positions", {"portfolio_id": env["pid"]})
    assert D(positions[0]["qty"]) == D("0.06")

    is_error, closed = await env["call"]("close_all_positions", {"portfolio_id": env["pid"]})
    assert not is_error, closed
    [o] = closed
    assert o["status"] == "filled" and D(o["qty"]) == D("0.06")
    _, positions = await env["call"]("get_positions", {"portfolio_id": env["pid"]})
    assert positions == []
