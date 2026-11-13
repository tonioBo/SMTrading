"""Dashboard real time: the changes the web app refreshes on are queued as events."""

from decimal import Decimal

import pytest

from smtrading.common.errors import SMTError
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import POST_COMMIT_KEY, discard_post_commit, notify
from smtrading.portfolios.service import create_portfolio, deposit, withdraw
from smtrading.seed import run_seed

D = Decimal


def _events(session) -> list[tuple[str, dict]]:
    return [(e[1], e[2]) for e in session.info.get(POST_COMMIT_KEY, []) if e[0] == "event"]


async def test_portfolio_created_and_cash_moves_are_published(db_session):
    p = await create_portfolio(db_session, name="EV-1", initial_cash=D(100))
    types = [t for t, _ in _events(db_session)]
    assert "portfolio.created" in types and "portfolio.cash" in types
    discard_post_commit(db_session)
    await deposit(db_session, p.id, D(5), "EUR", author_id="a")
    await withdraw(db_session, p.id, D(2), "EUR", author_id="a")
    cash = [e for t, e in _events(db_session) if t == "portfolio.cash"]
    assert [(e["type"], e["portfolio_id"]) for e in cash] == [
        ("deposit", p.id), ("withdrawal", p.id)]


async def test_notifications_are_published(db_session):
    p = await create_portfolio(db_session, name="EV-2", initial_cash=D(0))
    discard_post_commit(db_session)
    await notify(db_session, portfolio_id=p.id, kind="price_alert", payload={"x": 1})
    assert _events(db_session) == [
        ("notification", {"kind": "price_alert", "portfolio_id": p.id, "agent_id": None})]


async def test_order_accepted_and_rejected_are_published(db_session):
    await run_seed(db_session)
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    p = await create_portfolio(db_session, name="EV-3", initial_cash=D(1000))
    discard_post_commit(db_session)
    from datetime import UTC, datetime

    now = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
        qty=D(1), limit_price=D(100)), now=now)
    with pytest.raises(SMTError) as e:  # the persisted rejection is raised
        await place_order(db_session, qs, PlaceOrderRequest(
            portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
            qty=D(1000), limit_price=D(100)), now=now)
    assert e.value.persisted
    types = [t for t, _ in _events(db_session)]
    assert "order.accepted" in types and "order.rejected" in types
