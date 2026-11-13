"""I2: no QuoteSource call once a transaction has posted its first journal (the chain
lock is held from then until commit)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from smtrading.engine import execution as execution_module
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource, MemoQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_all_positions,
    place_oco_order,
    place_order,
)
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.margin import liquidate
from smtrading.seed import run_seed

D = Decimal
TUE = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # NASDAQ, MTA and XETRA open


class CountingSource(FakeQuoteSource):
    """Records every call, and every call made after the first post_journal."""

    def __init__(self, state: dict) -> None:
        super().__init__()
        self.state = state
        self.calls: list[tuple] = []
        self.late: list[tuple] = []

    def _record(self, call: tuple) -> None:
        self.calls.append(call)
        if self.state["journaled"]:
            self.late.append(call)

    async def get_quotes(self, symbols, *, max_age_s=5):
        self._record(("quotes", tuple(symbols)))
        return await super().get_quotes(symbols, max_age_s=max_age_s)

    async def get_fx_rate(self, base, quote):
        self._record(("fx", base, quote))
        return await super().get_fx_rate(base, quote)


@pytest.fixture
def state(monkeypatch):
    state = {"journaled": False}
    real = execution_module.post_journal

    async def post_journal(*args, **kwargs):
        state["journaled"] = True
        return await real(*args, **kwargs)

    monkeypatch.setattr(execution_module, "post_journal", post_journal)
    return state


@pytest.fixture
def qs(state):
    q = CountingSource(state)
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None)
    q.set_quote("SAP.DE", D("99.99"), D("100.01"), None)
    return q


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


async def _portfolio(session, leverage=1):
    return await create_portfolio(
        session, name=f"L-{uuid.uuid4().hex[:8]}", initial_cash=D(100000),
        leverage=D(leverage),
    )


def _req(p, symbol, side=OrderSide.buy, qty=10, **kw):
    return PlaceOrderRequest(
        portfolio_id=p.id, symbol=symbol, side=side, type=kw.pop("type", OrderType.market),
        qty=D(qty), **kw,
    )


def _reset(qs, state):
    """New transaction from the test's point of view: forget the flag and the calls."""
    state["journaled"] = False
    qs.calls.clear()
    qs.late.clear()


async def test_place_order_with_fx_and_daily_loss_check(db_session, seeded, qs, state):
    p = await _portfolio(db_session)
    await place_order(db_session, qs, _req(p, "ENI.MI"), now=TUE)
    _reset(qs, state)

    order = await place_order(db_session, qs, _req(p, "AAPL"), now=TUE)  # EUR -> USD fx

    assert order.status == OrderStatus.filled
    assert state["journaled"]
    assert qs.calls and qs.late == []


async def test_close_all_positions(db_session, seeded, qs, state):
    p = await _portfolio(db_session)
    for symbol in ("AAPL", "ENI.MI", "SAP.DE"):
        await place_order(db_session, qs, _req(p, symbol), now=TUE)
    _reset(qs, state)

    orders = await close_all_positions(db_session, qs, p.id, now=TUE)

    assert [o.status for o in orders] == [OrderStatus.filled] * 3
    assert qs.late == []


async def test_oco_leg_fills_at_placement(db_session, seeded, qs, state):
    p = await _portfolio(db_session)
    await place_order(db_session, qs, _req(p, "AAPL"), now=TUE)
    _reset(qs, state)

    a, b = await place_oco_order(
        db_session, qs,
        _req(p, "AAPL", side=OrderSide.sell, type=OrderType.limit, limit_price=D("99.00")),
        _req(p, "AAPL", side=OrderSide.sell, type=OrderType.stop, stop_price=D("90.00")),
        now=TUE,
    )

    assert a.status == OrderStatus.filled and b.status == OrderStatus.canceled
    assert qs.late == []


async def test_liquidation_of_several_positions(db_session, seeded, qs, state):
    p = await _portfolio(db_session, leverage=4)
    await place_order(db_session, qs, _req(p, "ENI.MI", qty=D(20000)), now=TUE)
    await place_order(db_session, qs, _req(p, "SAP.DE", qty=D(900)), now=TUE)
    qs.set_quote("ENI.MI", D("9.995"), D("10.005"), None)
    qs.set_quote("SAP.DE", D("49.99"), D("50.01"), None)
    _reset(qs, state)

    closed = await liquidate(db_session, qs, p.id, now=TUE)

    assert len(closed) == 2
    assert qs.late == []


async def test_execute_fill_with_a_plain_source_prefetches(db_session, seeded, qs, state):
    """The processor path: execute_fill wraps the raw source itself."""
    p = await _portfolio(db_session)
    await place_order(db_session, qs, _req(p, "ENI.MI"), now=TUE)
    order = Order(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(10), limit_price=D(101), status=OrderStatus.pending,
    )
    db_session.add(order)
    await db_session.flush()
    quote = (await qs.get_quotes(["AAPL"]))["AAPL"]
    _reset(qs, state)

    result = await execute_fill(db_session, qs, order.id, quote=quote)

    assert result is not None and qs.late == []
    assert ("quotes", ("AAPL", "ENI.MI")) in qs.calls  # valuation prefetched up front


async def test_frozen_memo_refuses_misses():
    source = FakeQuoteSource()
    source.set_quote("AAPL", D(1), D(2), None)
    memo = MemoQuoteSource(source)
    await memo.prefetch(["AAPL", "NOPE"], {("EUR", "USD")})
    memo.freeze()
    assert (await memo.get_quotes(["AAPL"]))["AAPL"].bid == D(1)
    for call in (memo.get_quotes(["NOPE"]), memo.get_quotes(["MSFT"]),
                 memo.get_fx_rate("EUR", "USD")):
        with pytest.raises(Exception) as e:
            await call
        assert e.value.code == "PROVIDER_UNAVAILABLE"
    assert await memo.get_fx_rate("usd", "USD") == 1
    assert MemoQuoteSource.wrap(memo) is memo
