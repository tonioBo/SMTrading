"""Decisions with confidence and expected result; orders linked to their decision
(Neurograph plan, task 2). BTC-USD (market CRYPTO) is always open."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision
from smtrading.agents import service as agents
from smtrading.common.db import make_engine
from smtrading.engine.models import Order
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
    qs = mcp_server.qs
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote(SYM, D(50000), D(50000), D(50000))
    async with _Db(mcp_server.db_url) as s:
        await run_seed(s)
        other, _ = await agents.create_agent(s, "Altro")
        await s.flush()
        theirs = Decision(agent_id=other.id, kind="proposal", summary="non mia")
        s.add(theirs)
        await s.commit()

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    async def orders() -> list[Order]:
        async with _Db(mcp_server.db_url) as s:
            return list(await s.scalars(select(Order).order_by(Order.created_at)))

    async def decision(did: int) -> Decision:
        async with _Db(mcp_server.db_url) as s:
            return await s.get(Decision, did)

    return {"call": call, "pid": str(mcp_server.demo_id), "theirs": theirs.id,
            "orders": orders, "decision": decision, "db_url": mcp_server.db_url}


async def _decide(env, **kw) -> int:
    is_error, r = await env["call"]("log_decision", {"kind": "proposal", "summary": "compro",
                                                     **kw})
    assert not is_error, r
    return r["decision_id"]


async def test_decision_stores_and_returns_confidence_and_expected(env):
    did = await _decide(env, confidence=70, expected={"return_pct": 2.5, "horizon_days": 5})
    row = await env["decision"](did)
    assert row.confidence == 70
    assert row.expected == {"return_pct": 2.5, "horizon_days": 5}

    is_error, r = await env["call"]("get_decisions", {})
    assert not is_error, r
    [mine] = [d for d in r["decisions"] if d["id"] == did]
    assert mine["confidence"] == 70
    assert mine["expected"] == {"return_pct": 2.5, "horizon_days": 5}


async def test_decision_without_new_fields_unchanged(env):
    did = await _decide(env)
    row = await env["decision"](did)
    assert row.confidence is None and row.expected is None
    _, r = await env["call"]("get_decisions", {})
    [mine] = [d for d in r["decisions"] if d["id"] == did]
    assert mine["confidence"] is None and mine["expected"] is None


@pytest.mark.parametrize("args,field", [
    ({"confidence": 101}, "confidence"),
    ({"confidence": -1}, "confidence"),
    ({"confidence": 50.5}, "confidence"),
    ({"expected": {"return_pct": 2.5}}, "expected"),
    ({"expected": {"return_pct": 2.5, "horizon_days": 0}}, "expected"),
    ({"expected": {"return_pct": 2.5, "horizon_days": 366}}, "expected"),
    ({"expected": {"return_pct": 2.5, "horizon_days": 2.5}}, "expected"),
    ({"expected": {"return_pct": "molto", "horizon_days": 5}}, "expected"),
    ({"expected": {"return_pct": 2.5, "horizon_days": 5, "note": "x"}}, "expected"),
    ({"expected": [2.5, 5]}, "expected"),
])
async def test_decision_invalid_fields(env, args, field):
    is_error, err = await env["call"]("log_decision", {"kind": "proposal", "summary": "x",
                                                       **args})
    assert is_error and err["code"] == "INVALID_ORDER", err
    assert err["details"]["field"] == field


async def test_decision_confidence_bounds_accepted(env):
    for c in (0, 100):
        did = await _decide(env, confidence=c,
                            expected={"return_pct": -1, "horizon_days": 365})
        assert (await env["decision"](did)).confidence == c


async def test_place_order_stores_own_decision(env):
    did = await _decide(env)
    is_error, o = await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market",
        "qty": "0.1", "decision_id": did,
    })
    assert not is_error, o
    assert "decision_id" not in o  # the MCP order fields stay as they are
    [row] = await env["orders"]()
    assert str(row.id) == o["id"] and row.decision_id == did


async def test_place_order_without_decision_unchanged(env):
    is_error, o = await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market",
        "qty": "0.1",
    })
    assert not is_error, o
    [row] = await env["orders"]()
    assert row.decision_id is None


@pytest.mark.parametrize("which", ["theirs", "unknown", "garbage"])
async def test_foreign_or_unknown_decision_refused_and_nothing_stored(env, which):
    did = {"theirs": env["theirs"], "unknown": 999999999, "garbage": "abc"}[which]
    base = {"portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "decision_id": did}
    calls = [
        ("place_order", {**base, "type": "market", "qty": "0.1"}),
        ("place_bracket_order", {**base, "qty": "0.1", "take_profit": "60000",
                                 "stop_loss": "45000"}),
        ("place_oco_order", {**base, "qty": "0.1",
                             "a": {"type": "limit", "limit_price": "40000"},
                             "b": {"type": "stop", "stop_price": "60000"}}),
    ]
    for tool, args in calls:
        is_error, err = await env["call"](tool, args)
        assert is_error and err["code"] == "INVALID_ORDER", (tool, err)
        assert err["details"]["field"] == "decision_id", (tool, err)
    assert await env["orders"]() == []


async def test_bracket_and_oco_carry_decision(env):
    did = await _decide(env)
    is_error, orders = await env["call"]("place_bracket_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "qty": "0.1",
        "take_profit": "60000", "stop_loss": "45000", "decision_id": did,
    })
    assert not is_error, orders
    assert len(orders) == 3

    is_error, legs = await env["call"]("place_oco_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "sell", "qty": "0.05",
        "a": {"type": "limit", "limit_price": "70000"},
        "b": {"type": "stop", "stop_price": "40000"}, "decision_id": did,
    })
    assert not is_error, legs
    rows = await env["orders"]()
    assert len(rows) == 5 and {o.decision_id for o in rows} == {did}


async def test_close_tools_carry_decision(env):
    await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market",
        "qty": "0.1"})
    did = await _decide(env)
    is_error, o = await env["call"]("close_position", {
        "portfolio_id": env["pid"], "symbol": SYM, "qty": "0.04", "decision_id": did})
    assert not is_error, o
    by_id = {str(r.id): r.decision_id for r in await env["orders"]()}
    assert by_id[o["id"]] == did

    is_error, err = await env["call"]("close_all_positions", {
        "portfolio_id": env["pid"], "decision_id": env["theirs"]})
    assert is_error and err["details"]["field"] == "decision_id"

    is_error, closed = await env["call"]("close_all_positions", {
        "portfolio_id": env["pid"], "decision_id": did})
    assert not is_error, closed
    by_id = {str(r.id): r.decision_id for r in await env["orders"]()}
    assert len(closed) == 1 and by_id[closed[0]["id"]] == did
    assert len(by_id) == 3  # the refused call stored nothing


async def test_deleting_decision_unlinks_order(env):
    did = await _decide(env)
    await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": SYM, "side": "buy", "type": "market",
        "qty": "0.1", "decision_id": did})
    async with _Db(env["db_url"]) as s:
        await s.execute(Decision.__table__.delete().where(Decision.id == did))
        await s.commit()
        n = await s.scalar(select(func.count()).select_from(Order)
                           .where(Order.decision_id.is_(None)))
    assert n == 1
