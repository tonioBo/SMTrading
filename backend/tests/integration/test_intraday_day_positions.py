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
from smtrading.intraday.day_trades import own_cap_usage
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


async def _link(db_session, kind="alpaca_paper"):
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind)
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


async def test_own_usage_window_with_holiday(db_session, seeded):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=3)
    mon23 = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)
    mon30 = datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
    await _round_trip(db_session, p, mon23)
    await _round_trip(db_session, p, mon30)
    u = await own_cap_usage(db_session, p, mon30)
    assert u.used == 2 and u.frees_on == date(2026, 12, 1)
    u = await own_cap_usage(db_session, p, datetime(2026, 12, 1, 16, 0, tzinfo=UTC))
    assert u.used == 1


async def test_own_usage_pending_counts_open_day_positions_and_resting_entries(
        db_session, seeded):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=3)
    await _order(db_session, p, OrderSide.buy, 10, WED, day_trade=True)
    await _pos(db_session, p, 10)  # AAPL: open day position
    await _order(db_session, p, OrderSide.buy, 5, WED, symbol="MSFT", day_trade=True,
                 status=OrderStatus.queued, fill=False)  # resting entry
    await _order(db_session, p, OrderSide.buy, 5, WED, symbol="NVDA", day_trade=False,
                 status=OrderStatus.queued, fill=False)  # not a day trade: ignored
    u = await own_cap_usage(db_session, p, WED)
    assert (u.used, u.pending, u.remaining) == (0, 2, 1)


async def test_own_cap_usage_reaches_zero(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True, max_day_trades_5d=1)
    await _round_trip(db_session, p, WED)
    u = await own_cap_usage(db_session, p, WED, "AAPL")
    assert u.remaining == 0 and u.limit == 1


async def test_own_usage_without_cap(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True)
    await _round_trip(db_session, p, WED)
    u = await own_cap_usage(db_session, p, WED, "AAPL")
    assert u.limit is None and u.remaining is None
    assert u.used == 1  # the count is there without a cap too


async def test_own_cap_ignores_other_portfolios_of_link(db_session, seeded):
    link = await _link(db_session)
    mine = await _pf(db_session, link, intraday_enabled=True, max_day_trades_5d=1)
    other = await _pf(db_session, link, auto_rules_enabled=True)
    await _round_trip(db_session, other, WED)
    u = await own_cap_usage(db_session, mine, WED, "AAPL")
    assert u.remaining == 1 and u.used == 0


async def test_own_usage_same_symbol_adds_nothing(db_session, seeded):
    p = await _pf(db_session, intraday_enabled=True, max_day_trades_5d=1)
    await _order(db_session, p, OrderSide.buy, 10, WED, day_trade=True)
    await _pos(db_session, p, 10)
    assert (await own_cap_usage(db_session, p, WED, "AAPL")).remaining == 1
    assert (await own_cap_usage(db_session, p, WED, "MSFT")).remaining == 0
    assert (await own_cap_usage(db_session, p, WED)).remaining == 0  # portal: nothing released
