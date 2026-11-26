import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.intraday.models import DAY_SCOPE, day_scope
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import RiskProfile
from smtrading.seed import run_seed

OPEN = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # a Wednesday, NYSE open


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    return q


@pytest.fixture
async def p(db_session, seeded):
    return await create_portfolio(
        db_session, name=f"I-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(1),
    )


def _req(p, **kw):
    return PlaceOrderRequest(portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy,
                             type=OrderType.market, qty=D(1), **kw)


async def test_intraday_defaults_are_off(db_session, qs, p):
    await db_session.refresh(p)
    assert p.intraday_enabled is False
    assert p.auto_rules_enabled is False
    assert p.intraday_timeframes == ["15m", "1h"]
    assert p.day_close_minutes == 10
    assert p.intraday_max_wakes_per_hour == 6
    assert p.intraday_wake_agent_id is None
    assert p.max_day_trades_5d is None
    order = await place_order(db_session, qs, _req(p), now=OPEN)
    assert order.day_trade is False
    margin = await db_session.scalar(
        select(RiskProfile.day_close_limit_margin_pct).where(RiskProfile.name == "Default"))
    assert margin == D("0.5")


async def test_defaults_are_server_side(db_session, seeded):
    await db_session.execute(text("insert into portfolios (id, name, base_currency, leverage, maintenance_margin_pct,"
        " warning_margin_pct, slippage_bps, status, timezone) values (:i, 'SRV', 'EUR', 1, 50,"
        " 75, 0, 'active', 'Europe/Rome')"),
                             {"i": uuid.uuid4()})
    row = (await db_session.execute(text(
        "select intraday_enabled, auto_rules_enabled, intraday_timeframes, day_close_minutes,"
        " intraday_max_wakes_per_hour from portfolios where name = 'SRV'"))).one()
    assert tuple(row) == (False, False, ["15m", "1h"], 10, 6)


@pytest.mark.parametrize("a,b,expected", [(False, False, False), (True, False, True),
                                          (False, True, True), (True, True, True)])
async def test_day_scope(db_session, seeded, a, b, expected):
    pf = Portfolio(name=f"S-{uuid.uuid4().hex[:8]}", intraday_enabled=a, auto_rules_enabled=b)
    db_session.add(pf)
    await db_session.flush()
    assert day_scope(pf) is expected
    ids = set(await db_session.scalars(select(Portfolio.id).where(DAY_SCOPE)))
    assert (pf.id in ids) is expected


async def test_day_trade_propagates_to_order(db_session, qs, p):
    order = await place_order(db_session, qs, _req(p, day_trade=True), now=OPEN)
    assert order.day_trade is True
    await db_session.refresh(order)
    assert order.day_trade is True


async def test_client_order_id_reused_with_other_day_trade_is_invalid(db_session, qs, p):
    first = await place_order(db_session, qs, _req(p, client_order_id="dt-1"), now=OPEN)
    again = await place_order(db_session, qs, _req(p, client_order_id="dt-1"), now=OPEN)
    assert again.id == first.id
    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, _req(p, client_order_id="dt-1", day_trade=True),
                          now=OPEN)
    assert exc.value.code == ErrorCode.INVALID_ORDER
    assert ErrorCode.DAY_TRADE_LIMIT == "DAY_TRADE_LIMIT"
    assert await db_session.scalar(
        select(Order.day_trade).where(Order.id == first.id)) is False


@pytest.mark.parametrize("sql", [
    "update portfolios set day_close_minutes = 1",
    "update portfolios set day_close_minutes = 61",
    "update portfolios set intraday_max_wakes_per_hour = 0",
    "update portfolios set intraday_max_wakes_per_hour = 61",
    "update portfolios set max_day_trades_5d = -1",
    "update risk_profiles set day_close_limit_margin_pct = 0",
    "update risk_profiles set day_close_limit_margin_pct = 5.1",
])
async def test_intraday_checks(db_session, p, sql):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text(sql))


async def test_trigger_rule_and_outcome_checks(db_session, p):
    def insert(rule, outcome):
        return db_session.execute(text(
            "insert into intraday_triggers (portfolio_id, strategy_ref, strategy_name, symbol,"
            " rule, timeframe, bar_close, rule_text, outcome) values (:p, 'r', 'n', 'AAPL',"
            " :rule, '15m', now(), 't', :o)"), {"p": p.id, "rule": rule, "o": outcome})

    await insert("entry", "pending")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await insert("both", "pending")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await insert("exit", "bogus")


async def test_downgrade_0046_and_back(fresh_db):
    from test_migrations import _alembic

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0045")
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
