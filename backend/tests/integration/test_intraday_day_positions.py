import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.intraday.day_positions import day_quantities
from smtrading.intraday.pdt import entry_budget, link_budget
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed

WED = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # NYSE open
TUE = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


async def _pf(db_session, link=None, **kw):
    p = await create_portfolio(db_session, name=f"D-{uuid.uuid4().hex[:8]}",
                               initial_cash=D(100000), leverage=D(1))
    p.broker_link_id = link.id if link else None
    for k, v in kw.items():
        setattr(p, k, v)
    await db_session.flush()
    return p


async def _link(db_session, kind="alpaca_paper", state=None):
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, pdt_state=state)
    db_session.add(link)
    await db_session.flush()
    return link


async def _order(db_session, p, side, qty, at, *, symbol="AAPL", day_trade=False,
                 status=OrderStatus.filled, link=None, fill=True, reduce_only=False):
    o = Order(portfolio_id=p.id, symbol=symbol, side=side, type=OrderType.market,
              qty=D(qty), day_trade=day_trade, status=status, reduce_only=reduce_only,
              broker_link_id=link.id if link else p.broker_link_id)
    db_session.add(o)
    await db_session.flush()
    if fill:
        h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                    hash=hashlib.sha256(h.encode()).hexdigest())
        db_session.add(j)
        await db_session.flush()
        db_session.add(Fill(order_id=o.id, portfolio_id=p.id, symbol=symbol, side=side,
                            qty=D(qty), price=D(100), fee_currency="USD", journal_id=j.id,
                            created_at=at))
        await db_session.flush()
    return o


async def _pos(db_session, p, qty, symbol="AAPL"):
    db_session.add(Position(portfolio_id=p.id, symbol=symbol, qty=D(qty)))
    await db_session.flush()


async def _round_trip(db_session, p, at, symbol="AAPL"):
    await _order(db_session, p, OrderSide.buy, 10, at, symbol=symbol, day_trade=True)
    await _order(db_session, p, OrderSide.sell, 10, at, symbol=symbol)


async def test_day_quantities_mixed_position(db_session, seeded):
    p = await _pf(db_session)
    await _order(db_session, p, OrderSide.buy, 60, TUE)
    await _order(db_session, p, OrderSide.buy, 40, WED, day_trade=True)
    await _pos(db_session, p, 100)
    assert await day_quantities(db_session, p.id, WED) == {"AAPL": D(40)}
    await _order(db_session, p, OrderSide.sell, 50, WED)
    await _pos_update(db_session, p, 50)
    assert await day_quantities(db_session, p.id, WED) == {}


async def _pos_update(db_session, p, qty):
    pos = await db_session.get(Position, (p.id, "AAPL"))
    pos.qty = D(qty)
    await db_session.flush()


async def test_day_quantities_ignores_non_day_trade_buys(db_session, seeded):
    p = await _pf(db_session)
    await _order(db_session, p, OrderSide.buy, 40, WED, day_trade=False)
    await _pos(db_session, p, 40)
    assert await day_quantities(db_session, p.id, WED) == {}


async def test_day_quantities_capped_by_position(db_session, seeded):
    p = await _pf(db_session)
    await _order(db_session, p, OrderSide.buy, 40, WED, day_trade=True)
    await _pos(db_session, p, 25)
    assert await day_quantities(db_session, p.id, WED) == {"AAPL": D(25)}


async def test_link_budget_counts_all_portfolios_of_the_link(db_session, seeded):
    link = await _link(db_session)
    a = await _pf(db_session, link, intraday_enabled=True)
    b = await _pf(db_session, link, auto_rules_enabled=True)
    await _round_trip(db_session, a, WED)
    await _round_trip(db_session, b, WED)
    bud = await link_budget(db_session, link.id, WED)
    assert (bud.applies, bud.limit, bud.used, bud.remaining) == (True, 3, 2, 1)


async def test_link_budget_window_with_holiday(db_session, seeded):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True)
    mon23 = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
    mon30 = datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
    await _round_trip(db_session, p, mon23)
    await _round_trip(db_session, p, mon30)
    b = await link_budget(db_session, link.id, mon30)
    assert b.used == 2 and b.frees_on == date(2026, 12, 1)
    b = await link_budget(db_session, link.id, datetime(2026, 12, 1, 16, 0, tzinfo=UTC))
    assert b.used == 1


async def test_pending_counts_open_day_positions_and_resting_entries(db_session, seeded):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True)
    await _order(db_session, p, OrderSide.buy, 10, WED, day_trade=True)
    await _pos(db_session, p, 10)  # AAPL: open day position
    await _order(db_session, p, OrderSide.buy, 5, WED, symbol="MSFT", day_trade=True,
                 status=OrderStatus.queued, fill=False)  # resting entry
    await _order(db_session, p, OrderSide.buy, 5, WED, symbol="NVDA", day_trade=False,
                 status=OrderStatus.queued, fill=False)  # not a day trade: ignored
    bud = await link_budget(db_session, link.id, WED)
    assert (bud.used, bud.pending, bud.remaining) == (0, 2, 1)


async def test_mismatch_with_broker_count(db_session, seeded):
    link = await _link(db_session, state={"daytrade_count": 2})
    p = await _pf(db_session, link, intraday_enabled=True)
    await _round_trip(db_session, p, WED)
    bud = await link_budget(db_session, link.id, WED)
    assert bud.used == 1 and bud.broker_count == 2 and bud.mismatch is True


async def test_entry_budget_own_cap(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True, max_day_trades_5d=1)
    await _round_trip(db_session, p, WED)
    bud = await entry_budget(db_session, p, "AAPL", WED)
    assert bud.remaining == 0 and bud.limit == 1


async def test_entry_budget_without_any_limit(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True)
    assert (await entry_budget(db_session, p, "AAPL", WED)).remaining is None


async def test_own_cap_ignores_other_portfolios_of_link(db_session, seeded):
    link = await _link(db_session, state={"multiplier": "2", "equity": "30000",
                                          "last_equity": "30000"})
    mine = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=1)
    other = await _pf(db_session, link, auto_rules_enabled=True)
    await _round_trip(db_session, other, WED)
    bud = await entry_budget(db_session, mine, "AAPL", WED)
    assert bud.remaining == 1  # the link has no PDT limit (equity >= 25000), own cap 1 unused


async def test_entry_budget_same_symbol_adds_nothing(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True, max_day_trades_5d=1)
    await _order(db_session, p, OrderSide.buy, 10, WED, day_trade=True)
    await _pos(db_session, p, 10)
    assert (await entry_budget(db_session, p, "AAPL", WED)).remaining == 1
    assert (await entry_budget(db_session, p, "MSFT", WED)).remaining == 0
