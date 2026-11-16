"""Attribution of an order to a strategy (STR-xxx) through the agents' decisions, with the
shapes found in production: the PM proposal (`details.ref` = the order's client_order_id,
`details.strategy_id`), the RM approval (`details.strategy_decision_id`), bracket legs
(`parent_id`) and OCO legs placed later by a decision that names their id."""

import uuid
from decimal import Decimal

import pytest

from smtrading.activity.models import Decision
from smtrading.agents.service import create_agent
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.strategies.attribution import strategy_of_order

D = Decimal
COID = "PROP-AGG-20261116-2130-1"


@pytest.fixture
async def ctx(db_session, portfolio):
    if await db_session.get(Instrument, "AMZN") is None:
        db_session.add(Instrument(
            symbol="AMZN", name="Amazon", asset_class=AssetClass.us_equity, market="NASDAQ",
            currency="USD", lot_size=D("0.000001"), tick_size=D("0.01"), shortable=True,
        ))
    agent, _ = await create_agent(db_session, f"PM {uuid.uuid4().hex[:6]}")
    await db_session.flush()
    return db_session, portfolio, agent


def _order(portfolio, **kw) -> Order:
    base = {"portfolio_id": portfolio.id, "symbol": "AMZN", "side": OrderSide.buy,
              "type": OrderType.limit, "qty": D("0.51"), "limit_price": D(258),
              "status": OrderStatus.filled}
    return Order(**{**base, **kw})


def _decision(agent, portfolio, kind, details, subject=None) -> Decision:
    return Decision(agent_id=agent.id, portfolio_id=portfolio.id, kind=kind,
                    summary=subject or kind, details=details, subject=subject)


async def _add(session, *rows):
    session.add_all(rows)
    await session.flush()
    return rows


async def test_proposal_strategy_id(ctx):
    s, p, a = ctx
    await _add(s, _decision(a, p, "proposal", {"ref": COID, "strategy_id": "STR-103"}))
    (order,) = await _add(s, _order(p, client_order_id=COID))
    assert await strategy_of_order(s, order) == "STR-103"


async def test_approval_strategy_decision_id(ctx):
    s, p, a = ctx
    (strategy,) = await _add(s, _decision(a, p, "strategy", {"ref": "STR-104"}))
    await _add(s, _decision(a, p, "approval", {"ref": COID,
                                               "strategy_decision_id": strategy.id}))
    (order,) = await _add(s, _order(p, client_order_id=COID))
    assert await strategy_of_order(s, order) == "STR-104"


async def test_ref_in_subject(ctx):
    s, p, a = ctx
    await _add(s, _decision(a, p, "approval", {"ref": COID},
                            subject="acquisto Amazon (strategia STR-103)"))
    (order,) = await _add(s, _order(p, client_order_id=COID))
    assert await strategy_of_order(s, order) == "STR-103"


async def test_proposal_wins_over_other_decisions(ctx):
    s, p, a = ctx
    await _add(s, _decision(a, p, "other", {"ref": COID}, subject="vedi STR-999"),
               _decision(a, p, "proposal", {"ref": COID, "strategy_id": "STR-103"}))
    (order,) = await _add(s, _order(p, client_order_id=COID))
    assert await strategy_of_order(s, order) == "STR-103"


async def test_bracket_leg_through_parent(ctx):
    s, p, a = ctx
    await _add(s, _decision(a, p, "proposal", {"ref": COID, "strategy_id": "STR-103"}))
    (entry,) = await _add(s, _order(p, client_order_id=COID, role="entry"))
    (stop,) = await _add(s, _order(p, side=OrderSide.sell, type=OrderType.stop,
                                   stop_price=D("242.2"), limit_price=None,
                                   parent_id=entry.id, role="stop_loss"))
    assert await strategy_of_order(s, stop) == "STR-103"


async def test_oco_leg_named_by_a_decision(ctx):
    s, p, a = ctx
    await _add(s, _decision(a, p, "proposal", {"ref": COID, "strategy_id": "STR-103"}))
    (stop,) = await _add(s, _order(p, side=OrderSide.sell, type=OrderType.stop,
                                   stop_price=D("242.2"), limit_price=None,
                                   oco_group_id=uuid.uuid4(), status=OrderStatus.pending))
    await _add(s, _decision(a, p, "other", {
        "ref": COID, "active_orders": {"stop": str(stop.id)}}))
    assert await strategy_of_order(s, stop) == "STR-103"


async def test_oco_sibling_through_group_id(ctx):
    s, p, a = ctx
    group = uuid.uuid4()
    await _add(s, _decision(a, p, "proposal", {"ref": COID, "strategy_id": "STR-103"}),
               _decision(a, p, "other", {"ref": COID, "active_orders": {
                   "oco_group": str(group)}}))
    (tp,) = await _add(s, _order(p, side=OrderSide.sell, oco_group_id=group,
                                 status=OrderStatus.pending))
    assert await strategy_of_order(s, tp) == "STR-103"


async def test_unknown(ctx):
    s, p, _a = ctx
    (plain,) = await _add(s, _order(p))
    (other,) = await _add(s, _order(p, client_order_id="manual-1"))
    assert await strategy_of_order(s, plain) is None
    assert await strategy_of_order(s, other) is None


async def test_other_portfolio_decisions_ignored(ctx, db_session):
    s, p, a = ctx
    from smtrading.portfolios.models import Portfolio
    q = Portfolio(name="Altro")
    s.add(q)
    await s.flush()
    await _add(s, _decision(a, q, "proposal", {"ref": COID, "strategy_id": "STR-103"}))
    (order,) = await _add(s, _order(p, client_order_id=COID))
    assert await strategy_of_order(s, order) is None


async def test_client_order_id_is_a_strategy_ref(ctx):
    s, p, _a = ctx
    (order,) = await _add(s, _order(p, client_order_id="STR-107"))
    assert await strategy_of_order(s, order) == "STR-107"
