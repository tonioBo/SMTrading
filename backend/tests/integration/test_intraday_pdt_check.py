import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import event, func, select

from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order, preview_order
from smtrading.intraday.pdt import warn_day_trade_sell
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

WED = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # NYSE open
MON = datetime(2026, 11, 23, 17, 0, tzinfo=UTC)
TUE = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)
UNDER = {"multiplier": "2", "equity": "10000", "last_equity": "10000"}


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)
    await db_session.flush()


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    for s in ("AAPL", "MSFT", "NVDA"):
        q.set_quote(s, D("199.90"), D("200.00"), D("199.95"))
    return q


async def _pf(db_session, link=None, **kw):
    p = await create_portfolio(db_session, name=f"P-{uuid.uuid4().hex[:8]}",
                               initial_cash=D(100000), leverage=D(1))
    p.broker_link_id = link.id if link else None
    for k, v in kw.items():
        setattr(p, k, v)
    await db_session.flush()
    return p


async def _link(db_session, state=UNDER, kind="alpaca_paper"):
    link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, pdt_state=state)
    db_session.add(link)
    await db_session.flush()
    return link


async def _order(db_session, p, side, qty, at, *, symbol="AAPL", day_trade=False,
                 status=OrderStatus.filled, fill=True, reduce_only=False):
    o = Order(portfolio_id=p.id, symbol=symbol, side=side, type=OrderType.market,
              qty=D(qty), day_trade=day_trade, status=status, reduce_only=reduce_only,
              broker_link_id=p.broker_link_id)
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


async def _round_trips(db_session, p, n, at=MON, symbol="NVDA"):
    for _ in range(n):
        await _order(db_session, p, OrderSide.buy, 1, at, symbol=symbol, day_trade=True)
        await _order(db_session, p, OrderSide.sell, 1, at, symbol=symbol)


def _req(p, symbol="AAPL", side=OrderSide.buy, **kw):
    kw.setdefault("qty", D(1))
    return PlaceOrderRequest(portfolio_id=p.id, symbol=symbol, side=side,
                             type=OrderType.market, **kw)


async def _preview(db_session, qs, req):
    return await preview_order(db_session, qs, req, now=WED)


async def _full_budget(db_session, **kw):
    link = await _link(db_session)
    p = await _pf(db_session, link, **kw)
    await _round_trips(db_session, p, 3)
    return link, p


async def test_fourth_day_trade_entry_refused(db_session, seeded, qs):
    _, p = await _full_budget(db_session, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT
    assert r.details["remaining"] == 0 and r.details["used"] == 3 and r.details["limit"] == 3
    assert "ne restano 0" in r.message and "il 01/12" in r.message  # Mon 23 + 5 sessions (Thanksgiving)
    assert r.details["frees_on"] == "2026-12-01"
    with pytest.raises(SMTError) as e:
        await place_order(db_session, qs, _req(p, day_trade=True), now=WED)
    assert e.value.code == ErrorCode.DAY_TRADE_LIMIT


async def test_auto_rules_portfolio_entry_refused(db_session, seeded, qs):
    _, p = await _full_budget(db_session, auto_rules_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT


async def test_budget_sums_both_modes(db_session, seeded, qs):
    link = await _link(db_session)
    a = await _pf(db_session, link, intraday_enabled=True)
    b = await _pf(db_session, link, auto_rules_enabled=True)
    await _round_trips(db_session, a, 1)
    await _round_trips(db_session, b, 1)
    await _order(db_session, b, OrderSide.buy, 1, WED, symbol="MSFT", day_trade=True)
    db_session.add(Position(portfolio_id=b.id, symbol="MSFT", qty=D(1)))
    await db_session.flush()
    for p in (a, b):
        r = await _preview(db_session, qs, _req(p, symbol="AAPL", day_trade=True))
        assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT


async def test_entry_refused_when_pending_fill_the_budget(db_session, seeded, qs):
    link = await _link(db_session)
    p = await _pf(db_session, link, intraday_enabled=True)
    await _round_trips(db_session, p, 2)
    await _order(db_session, p, OrderSide.buy, 1, WED, symbol="MSFT", day_trade=True)
    db_session.add(Position(portfolio_id=p.id, symbol="MSFT", qty=D(1)))
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, symbol="AAPL", day_trade=True))
    assert not r.ok and r.code == ErrorCode.DAY_TRADE_LIMIT
    assert "più 1 già aperte" in r.message
    r = await _preview(db_session, qs, _req(p, symbol="MSFT", day_trade=True))
    assert r.ok


async def test_sells_never_refused(db_session, seeded, qs):
    _, p = await _full_budget(db_session, intraday_enabled=True)
    p.broker_link_id = None  # a position through a local fill, then back on the link
    held = await place_order(db_session, qs, _req(p, qty=D(5)), now=WED)
    assert held.status == OrderStatus.filled
    link = await db_session.get(BrokerLink, (await db_session.scalar(
        select(Order.broker_link_id).where(Order.portfolio_id == p.id).limit(1))))
    p.broker_link_id = link.id
    await db_session.flush()
    r = await _preview(db_session, qs, _req(p, side=OrderSide.sell, reduce_only=True,
                                            day_trade=True))
    assert r.ok


async def test_non_day_trade_buy_not_checked(db_session, seeded, qs):
    _, p = await _full_budget(db_session, intraday_enabled=True)
    assert (await _preview(db_session, qs, _req(p, day_trade=False))).ok


async def test_cash_account_not_limited(db_session, seeded, qs):
    link = await _link(db_session, state={"multiplier": "1", "equity": "10000",
                                          "last_equity": "10000"})
    p = await _pf(db_session, link, intraday_enabled=True)
    await _round_trips(db_session, p, 3)
    assert (await _preview(db_session, qs, _req(p, day_trade=True))).ok


class _Count:
    def __init__(self, engine):
        self.engine, self.n = engine, 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *args):
        self.n += 1


async def test_off_portfolio_never_refused(db_session, seeded, qs):
    link = await _link(db_session)
    on = await _pf(db_session, link, intraday_enabled=True)
    await _round_trips(db_session, on, 3)
    off = await _pf(db_session, link)  # both switches off
    engine = db_session.bind.sync_engine
    with _Count(engine) as plain:
        assert (await _preview(db_session, qs, _req(off, day_trade=False))).ok
    with _Count(engine) as flagged:
        assert (await _preview(db_session, qs, _req(off, day_trade=True))).ok
    assert flagged.n == plain.n  # no PDT query for a portfolio out of day_scope


async def _outbox(db_session):
    return list((await db_session.scalars(select(TelegramOutbox))).all())


async def test_off_portfolio_sell_warns_on_shared_link(db_session, seeded):
    link = await _link(db_session)
    on = await _pf(db_session, link, intraday_enabled=True)
    off = await _pf(db_session, link)
    await _round_trips(db_session, on, 2)
    await _order(db_session, off, OrderSide.buy, 5, WED, symbol="AAPL")
    sell = await _order(db_session, off, OrderSide.sell, 5, WED, symbol="AAPL",
                        status=OrderStatus.pending, fill=False)
    await warn_day_trade_sell(db_session, sell, WED)
    rows = await _outbox(db_session)
    assert len(rows) == 1
    assert (rows[0].source, rows[0].category, rows[0].level) == ("system", "system", "warning")
    assert "operazione di giornata n. 3 su 3" in rows[0].text and "AAPL" in rows[0].text


async def test_no_warning_without_day_scope_on_link(db_session, seeded):
    link = await _link(db_session)
    a = await _pf(db_session, link)
    b = await _pf(db_session, link)
    await _round_trips(db_session, a, 2)
    await _order(db_session, b, OrderSide.buy, 5, WED, symbol="AAPL")
    sell = await _order(db_session, b, OrderSide.sell, 5, WED, symbol="AAPL",
                        status=OrderStatus.pending, fill=False)
    await warn_day_trade_sell(db_session, sell, WED)
    assert await _outbox(db_session) == []


async def test_no_warning_when_sell_closes_overnight_shares(db_session, seeded):
    link = await _link(db_session)
    on = await _pf(db_session, link, intraday_enabled=True)
    off = await _pf(db_session, link)
    await _round_trips(db_session, on, 2)
    await _order(db_session, off, OrderSide.buy, 5, TUE, symbol="AAPL")
    sell = await _order(db_session, off, OrderSide.sell, 5, WED, symbol="AAPL",
                        status=OrderStatus.pending, fill=False)
    await warn_day_trade_sell(db_session, sell, WED)
    assert await _outbox(db_session) == []


async def test_no_warning_without_link(db_session, seeded):
    off = await _pf(db_session)
    sell = await _order(db_session, off, OrderSide.sell, 5, WED, status=OrderStatus.pending,
                        fill=False)
    with _Count(db_session.bind.sync_engine) as c:
        await warn_day_trade_sell(db_session, sell, WED)
    assert c.n <= 1 and await db_session.scalar(select(func.count(TelegramOutbox.id))) == 0


async def test_preview_reports_day_trade_limit(db_session, seeded, qs):
    _, p = await _full_budget(db_session, intraday_enabled=True)
    r = await _preview(db_session, qs, _req(p, day_trade=True))
    assert r.ok is False and r.code == ErrorCode.DAY_TRADE_LIMIT
    assert await _outbox(db_session) == []
