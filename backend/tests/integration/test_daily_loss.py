import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.corporate.snapshots import ensure_day_open, take_snapshots
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk import daily_loss
from smtrading.risk.daily_loss import check_daily_loss, reset_daily
from smtrading.risk.models import EquitySnapshot, Notification, RiskProfile, SnapshotKind
from smtrading.seed import run_seed

D = Decimal
# Tuesday 11:00 in Rome: MTA open
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
ROME_MIDNIGHT = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)  # 2026-11-11 00:00 CET


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None)
    return q


async def _portfolio(session):
    return await create_portfolio(
        session, name=f"L-{uuid.uuid4().hex[:8]}", initial_cash=D(100000)
    )


async def _buy(session, qs, portfolio_id, qty):
    o = Order(
        portfolio_id=portfolio_id, symbol="ENI.MI", side=OrderSide.buy,
        type=OrderType.market, qty=D(qty), status=OrderStatus.new,
    )
    session.add(o)
    await session.flush()
    quote = (await qs.get_quotes(["ENI.MI"]))["ENI.MI"]
    assert await execute_fill(session, qs, o.id, quote=quote) is not None


async def test_daily_loss_sets_reduce_only_until_local_midnight(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _buy(db_session, qs, p.id, 6000)  # 90k in ENI
    day_open = await ensure_day_open(db_session, qs, p, NOW)
    assert day_open.local_date == date(2026, 11, 10)

    qs.set_quote("ENI.MI", D("13.495"), D("13.505"), None)  # -9k: 9% of ~99.9k
    assert await check_daily_loss(db_session, qs, p, NOW) is False
    assert p.status == PortfolioStatus.active

    qs.set_quote("ENI.MI", D("13.295"), D("13.305"), None)  # -10.2k: over 10%
    assert await check_daily_loss(db_session, qs, p, NOW) is True
    assert p.status == PortfolioStatus.reduce_only
    assert p.reduce_only_reason == "daily_loss"
    assert p.reduce_only_until == ROME_MIDNIGHT
    notes = list(await db_session.scalars(
        select(Notification).where(
            Notification.portfolio_id == p.id, Notification.kind == "daily_loss"
        )
    ))
    assert len(notes) == 1
    assert await check_daily_loss(db_session, qs, p, NOW) is False  # already blocked

    with pytest.raises(SMTError) as exc:
        await place_order(db_session, qs, PlaceOrderRequest(
            portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy, type=OrderType.market,
            qty=D(10),
        ), now=NOW)
    assert exc.value.code == ErrorCode.REDUCE_ONLY
    # closing is still allowed
    sell = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.sell, type=OrderType.market,
        qty=D(10),
    ), now=NOW)
    assert sell.status == OrderStatus.filled

    assert await reset_daily(db_session, ROME_MIDNIGHT - timedelta(seconds=1)) == 0
    assert await reset_daily(db_session, ROME_MIDNIGHT) == 1
    await db_session.refresh(p)
    assert p.status == PortfolioStatus.active
    assert p.reduce_only_reason is None and p.reduce_only_until is None


async def test_daily_loss_eur_limit(db_session, seeded, qs):
    profile = RiskProfile(name=f"R-{uuid.uuid4().hex[:8]}", max_daily_loss_eur=D(500))
    db_session.add(profile)
    p = await _portfolio(db_session)
    p.risk_profile_id = profile.id
    await db_session.flush()
    await _buy(db_session, qs, p.id, 1000)
    await ensure_day_open(db_session, qs, p, NOW)
    qs.set_quote("ENI.MI", D("14.395"), D("14.405"), None)  # -600
    assert await check_daily_loss(db_session, qs, p, NOW) is True


async def test_reset_daily_ignores_other_reasons(db_session, seeded):
    p = await _portfolio(db_session)
    p.status = PortfolioStatus.reduce_only
    p.reduce_only_reason = "admin"
    p.reduce_only_until = NOW
    await db_session.flush()
    assert await reset_daily(db_session, NOW + timedelta(days=1)) == 0


async def test_ensure_day_open_is_per_local_date(db_session, seeded, qs):
    p = await _portfolio(db_session)
    a = await ensure_day_open(db_session, qs, p, NOW)
    b = await ensure_day_open(db_session, qs, p, NOW + timedelta(hours=1))
    assert a.id == b.id and a.equity_eur == D("100000.00")
    c = await ensure_day_open(db_session, qs, p, ROME_MIDNIGHT)  # next day in Rome
    assert c.id != a.id and c.local_date == date(2026, 11, 11)


async def test_place_order_fill_checks_daily_loss(db_session, seeded, qs):
    p = await _portfolio(db_session)
    order = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy, type=OrderType.market,
        qty=D(10),
    ), now=NOW)
    assert order.status == OrderStatus.filled
    snap = await db_session.scalar(select(EquitySnapshot).where(
        EquitySnapshot.portfolio_id == p.id, EquitySnapshot.kind == SnapshotKind.day_open,
    ))
    assert snap is not None and snap.local_date == date(2026, 11, 10)


async def test_place_order_survives_a_failing_daily_loss_check(
    db_session, seeded, qs, monkeypatch, caplog
):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(daily_loss, "check_daily_loss", boom)
    p = await _portfolio(db_session)
    order = await place_order(db_session, qs, PlaceOrderRequest(
        portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy, type=OrderType.market,
        qty=D(10),
    ), now=NOW)
    assert order.status == OrderStatus.filled
    assert "daily loss check" in caplog.text


# -- real commits ------------------------------------------------------------------------


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


async def test_concurrent_ensure_day_open_creates_one_row(maker, qs):
    async with maker() as s:
        pid = (await _portfolio(s)).id
        await s.commit()

    async def one():
        async with maker() as s:
            p = await s.get(Portfolio, pid)
            snap = await ensure_day_open(s, qs, p, NOW)
            await asyncio.sleep(0.05)
            await s.commit()
            return snap.id

    ids = await asyncio.gather(*(one() for _ in range(5)))
    assert len(set(ids)) == 1
    async with maker() as s:
        assert await s.scalar(
            select(func.count()).select_from(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid)
        ) == 1


async def test_take_snapshots(maker, qs):
    async with maker() as s:
        pid = (await _portfolio(s)).id
        await s.commit()
    await take_snapshots(maker, qs, clock=lambda: NOW)
    await take_snapshots(maker, qs, clock=lambda: NOW + timedelta(minutes=5))
    async with maker() as s:
        rows = list(await s.scalars(
            select(EquitySnapshot).where(EquitySnapshot.portfolio_id == pid)
            .order_by(EquitySnapshot.id)
        ))
    kinds = sorted(r.kind for r in rows)
    assert kinds == [SnapshotKind.day_open, SnapshotKind.periodic, SnapshotKind.periodic]
    assert all(r.equity_eur == D("100000.00") for r in rows)
