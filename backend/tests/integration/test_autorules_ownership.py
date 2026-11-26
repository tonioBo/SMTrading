"""One owner per (portfolio, symbol): risk step 11 refuses a manual or agent buy on a pair
owned by an automatic rule (active, or paused with an open trade), lets the rule's own
orders and every closing order through, costs no query on a portfolio with the switch off;
mode A does not evaluate owned pairs. Names are made up."""
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import event, update
from test_intraday_job import Calls, _job, _portfolio, _strategy
from test_intraday_job import env as job_env  # noqa: F401  (fixture)

from smtrading.autorules import rules
from smtrading.autorules.ownership import owned_symbols
from smtrading.autorules.tables import (
    CLOSED,
    OPEN,
    AutoRule,
    AutoTrade,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, close_position, place_order
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)  # a Wednesday, NYSE open
BY = "admin@example.com"


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    for sym in ("AAPL", "MSFT"):
        q.set_quote(sym, D("190.00"), D("190.10"), D("190.05"))
    return q


async def _pf(session, *, auto=True) -> Portfolio:
    p = await create_portfolio(session, name=f"O-{uuid.uuid4().hex[:8]}",
                               initial_cash=D(100000), leverage=D(1))
    p.auto_rules_enabled = auto
    await session.flush()
    return p


async def _rule(session, p, *, active=True, symbol="AAPL") -> AutoRule:
    rule = await rules.create_rule(session, portfolio_id=p.id, symbol=symbol,
                                   model="breakout", params={"lookback_min": 20}, qty=D(1),
                                   by=BY)
    if active:
        await rules.activate(session, rule.id, by=BY, now=NOW)
        await session.flush()
    return rule


async def _trade(session, rule, status, key="k1") -> AutoTrade:
    t = AutoTrade(rule_id=rule.id, rule_version=1, portfolio_id=rule.portfolio_id,
                  symbol=rule.symbol, trigger_key=key, trigger_at=NOW,
                  trigger_price=D(190), trigger_values={}, expected_price=D(190),
                  status=status)
    session.add(t)
    await session.flush()
    return t


def _req(p, side=OrderSide.buy, source="admin", symbol="AAPL", **kw):
    kw.setdefault("qty", D(1))
    return PlaceOrderRequest(portfolio_id=p.id, symbol=symbol, side=side,
                             type=OrderType.market, source=source, **kw)


async def _refused(session, qs, req, reason):
    with pytest.raises(SMTError) as e:
        await place_order(session, qs, req, now=NOW)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert e.value.details["reason"] == reason
    return e.value


async def test_manual_buy_refused_while_rule_active(db_session, seeded, qs):
    p = await _pf(db_session)
    rule = await _rule(db_session, p)
    e = await _refused(db_session, qs, _req(p), "auto_rule_owner")
    assert "gestito da una regola automatica" in e.message
    assert e.details["rule_id"] == str(rule.id)


async def test_agent_buy_refused_while_rule_active(db_session, seeded, qs):
    p = await _pf(db_session)
    await _rule(db_session, p)
    await _refused(db_session, qs, _req(p, source="agent"), "auto_rule_owner")


async def test_other_symbol_is_free(db_session, seeded, qs):
    p = await _pf(db_session)
    await _rule(db_session, p)
    order = await place_order(db_session, qs, _req(p, symbol="MSFT"), now=NOW)
    assert order.status.value != "rejected"


async def test_rule_order_accepted(db_session, seeded, qs):
    p = await _pf(db_session)
    rule = await _rule(db_session, p)
    order = await place_order(db_session, qs, _req(p, auto_rule_id=rule.id), now=NOW)
    await db_session.refresh(order)
    assert order.status.value != "rejected" and order.auto_rule_id == rule.id


async def test_reduce_only_never_refused(db_session, seeded, qs):
    p = await _pf(db_session)
    rule = await _rule(db_session, p)
    bought = await place_order(db_session, qs, _req(p, auto_rule_id=rule.id, qty=D(4)), now=NOW)
    assert bought.status.value == "filled"
    sell = await place_order(db_session, qs, _req(p, OrderSide.sell, reduce_only=True,
                                                  qty=D(1)), now=NOW)
    assert sell.status.value != "rejected"
    closed = await close_position(db_session, qs, p.id, "AAPL", D(2), source="admin", now=NOW)
    assert closed.status.value != "rejected"


async def test_open_trade_keeps_ownership_after_pause(db_session, seeded, qs):
    p = await _pf(db_session)
    rule = await _rule(db_session, p)
    trade = await _trade(db_session, rule, OPEN)
    await rules.pause(db_session, rule.id, by=BY)
    await db_session.flush()
    await _refused(db_session, qs, _req(p), "auto_rule_owner")
    own = await place_order(db_session, qs, _req(p, auto_rule_id=rule.id), now=NOW)
    assert own.status.value != "rejected"  # the paused rule's own order still passes
    trade.status = CLOSED
    await db_session.flush()
    manual = await place_order(db_session, qs, _req(p, qty=D(2)), now=NOW)
    assert manual.status.value != "rejected"


async def test_inactive_rule_order_refused(db_session, seeded, qs):
    p = await _pf(db_session)
    rule = await _rule(db_session, p, active=False)
    await _refused(db_session, qs, _req(p, auto_rule_id=rule.id), "auto_rule_inactive")
    await rules.activate(db_session, rule.id, by=BY, now=NOW)
    await rules.pause(db_session, rule.id, by=BY)
    await db_session.flush()
    await _refused(db_session, qs, _req(p, auto_rule_id=rule.id), "auto_rule_inactive")


async def test_no_query_when_switch_off(db_session, seeded, qs):
    on = await _pf(db_session)
    await _rule(db_session, on)
    off = await _pf(db_session, auto=False)
    engine = db_session.bind.sync_engine
    statements: list[str] = []

    def count(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        order = await place_order(db_session, qs, _req(off, auto_rule_id=None), now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert order.status.value != "rejected"
    assert not [s for s in statements if re.search(r"\bauto_(rules|trades)\b", s)]


async def test_mode_a_skips_owned_symbol(job_env, monkeypatch):  # noqa: F811
    sm, _ = job_env
    calls = Calls(monkeypatch)
    pid = await _portfolio(sm, intraday=True, auto=True)
    await _strategy(sm, "O-1", ["JOBSPY", "JOBQQQ"], [pid])
    async with sm() as s:
        rule = await rules.create_rule(s, portfolio_id=pid, symbol="JOBSPY",
                                       model="breakout", params={"lookback_min": 20},
                                       qty=D(1), by=BY)
        await s.execute(update(AutoRule).where(AutoRule.id == rule.id).values(status="active"))
        await s.commit()
    async with sm() as s:
        assert await owned_symbols(s) == frozenset({(pid, "JOBSPY")})
    bar = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
    job, _ = _job(sm, bar + timedelta(seconds=65), exclude_provider=owned_symbols)
    await job.tick()
    assert calls.evaluations  # one per market; each gets the owned pair to skip
    assert {e[3] for e in calls.evaluations} == {frozenset({(pid, "JOBSPY")})}
