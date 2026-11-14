import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from smtrading.brokers.live.audit import record_live_audit
from smtrading.brokers.live.models import LiveAudit, LiveState
from smtrading.brokers.models import BrokerLink


@pytest.fixture
async def live_link(db_session):
    link = BrokerLink(
        id=uuid.uuid4(), name=f"live-{uuid.uuid4().hex[:6]}", kind="alpaca_live",
        live_state=LiveState.configured,
    )
    db_session.add(link)
    await db_session.flush()
    return link


async def test_live_link_columns_defaults(db_session, live_link):
    await db_session.refresh(live_link)
    assert live_link.live_state == "configured"
    assert live_link.health_failures == 0
    assert live_link.armed_at is None and live_link.account_fingerprint is None


async def test_live_state_check(db_session, live_link):
    with pytest.raises(DBAPIError, match="ck_broker_links_live_state"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("update broker_links set live_state = 'bogus' where id = :i"),
                {"i": live_link.id},
            )


async def test_live_audit_is_append_only(db_session, live_link):
    row = await record_live_audit(
        db_session, action="keys_saved", link_id=live_link.id, actor="admin@example.com",
        ip="127.0.0.1", user_agent="pytest",
    )
    assert row.id is not None and row.ts is not None
    for stmt in (
        "update live_audit set actor = 'x' where id = :i",
        "delete from live_audit where id = :i",
    ):
        with pytest.raises(DBAPIError, match="LIVE_AUDIT_APPEND_ONLY"):
            async with db_session.begin_nested():
                await db_session.execute(text(stmt), {"i": row.id})


async def test_live_audit_action_check(db_session, live_link):
    with pytest.raises(DBAPIError, match="ck_live_audit_action"):
        async with db_session.begin_nested():
            await record_live_audit(
                db_session, action="bogus", link_id=live_link.id, actor="system"
            )


async def test_live_audit_details_redacted(db_session, live_link):
    row = await record_live_audit(
        db_session, action="keys_saved", link_id=live_link.id, actor="system",
        details={
            "secret_key": "CANARYSECRET-do-not-use",
            "nested": {"api_key": "CANARYKEYID0001", "n": 1},
            "key_masked": "••••0001",
            "amount": __import__("decimal").Decimal("1.50"),
        },
    )
    stored = (
        await db_session.execute(select(LiveAudit.details).where(LiveAudit.id == row.id))
    ).scalar_one()
    assert stored["secret_key"] == "••••"
    assert stored["nested"] == {"api_key": "••••", "n": 1}
    assert stored["key_masked"] == "••••0001"  # an already masked value is kept
    assert stored["amount"] == "1.50"
    assert "CANARY" not in str(stored)


async def test_risk_profile_live_fuse_default_30(db_session):
    from smtrading.risk.models import RiskProfile

    p = RiskProfile(name=f"rp-{uuid.uuid4().hex[:6]}")
    db_session.add(p)
    await db_session.flush()
    await db_session.refresh(p)
    assert p.max_live_orders_per_hour == 30
    for bad in (0, 1001):
        with pytest.raises(DBAPIError, match="ck_risk_profiles_max_live_orders_per_hour"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text("update risk_profiles set max_live_orders_per_hour = :v where id = :i"),
                    {"v": bad, "i": p.id},
                )


async def test_order_live_flatten_default_false(db_session, portfolio):
    from smtrading.seed import run_seed

    await run_seed(db_session)
    oid = await db_session.scalar(text(
        "insert into orders (id, portfolio_id, symbol, side, type, tif, qty, filled_qty,"
        " status, source) values (gen_random_uuid(), :p, 'AAPL', 'buy', 'market', 'day', 1, 0,"
        " 'new', 'agent') returning id"
    ), {"p": portfolio.id})
    assert await db_session.scalar(
        text("select live_flatten from orders where id = :i"), {"i": oid}
    ) is False


def test_place_order_request_live_flatten_default():
    from smtrading.engine.service import PlaceOrderRequest

    assert PlaceOrderRequest.__dataclass_fields__["live_flatten"].default is False


async def test_place_order_propagates_live_flatten(db_session):
    from datetime import UTC, datetime
    from decimal import Decimal

    from smtrading.engine.models import OrderSide, OrderType
    from smtrading.engine.quotesource import FakeQuoteSource
    from smtrading.engine.service import PlaceOrderRequest, place_order
    from smtrading.portfolios.service import create_portfolio
    from smtrading.seed import run_seed

    await run_seed(db_session)
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", Decimal("1.25"))
    qs.set_quote("AAPL", Decimal("199.90"), Decimal("200.00"), Decimal("199.95"))
    p = await create_portfolio(
        db_session, name=f"F-{uuid.uuid4().hex[:8]}", initial_cash=Decimal(100000)
    )
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    flagged = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
        qty=Decimal(1), live_flatten=True,
    ), now=saturday)
    plain = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
        qty=Decimal(1),
    ), now=saturday)
    assert flagged.live_flatten is True
    assert plain.live_flatten is False
