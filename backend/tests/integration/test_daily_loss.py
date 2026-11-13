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
from smtrading.portfolios.service import create_portfolio, deposit, withdraw
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


async def test_daily_loss_exactly_at_the_limit(db_session, seeded, qs):
    p = await create_portfolio(
        db_session, name=f"L-{uuid.uuid4().hex[:8]}", initial_cash=D(90000)
    )
    db_session.add(EquitySnapshot(
        portfolio_id=p.id, ts=NOW - timedelta(hours=1), kind=SnapshotKind.day_open,
        local_date=date(2026, 11, 10), equity_eur=D("100000.00"), cash_eur=D("100000.00"),
        exposure_eur=D(0), margin_used_eur=D(0),
    ))
    await db_session.flush()
    assert await check_daily_loss(db_session, qs, p, NOW) is True  # exactly 10%
    assert p.status == PortfolioStatus.reduce_only


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


async def _day_open_now(maker, qs, pid):
    async with maker() as s:
        await ensure_day_open(s, qs, await s.get(Portfolio, pid), datetime.now(UTC))
        await s.commit()
    await asyncio.sleep(0.05)


async def test_withdrawal_is_not_a_daily_loss(maker, qs):
    async with maker() as s:
        pid = (await _portfolio(s)).id
        await s.commit()
    await _day_open_now(maker, qs, pid)
    async with maker() as s:
        await withdraw(s, pid, D(20000), "EUR", author_id="admin")
        await s.commit()
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        assert await check_daily_loss(s, qs, p, datetime.now(UTC)) is False
        assert p.status == PortfolioStatus.active


async def test_deposit_does_not_hide_a_daily_loss(maker, qs):
    async with maker() as s:
        pid = (await _portfolio(s)).id
        await _buy(s, qs, pid, 6000)
        await s.commit()
    await _day_open_now(maker, qs, pid)
    async with maker() as s:
        await deposit(s, pid, D(20000), "EUR", author_id="admin")
        await s.commit()
    qs.set_quote("ENI.MI", D("13.295"), D("13.305"), None)  # -10.2k
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        assert await check_daily_loss(s, qs, p, datetime.now(UTC)) is True
        await s.commit()
    async with maker() as s:
        note = await s.scalar(select(Notification).where(
            Notification.portfolio_id == pid, Notification.kind == "daily_loss"
        ))
        assert D(note.payload["net_cash_flows_eur"]) == D(20000)


async def test_reversed_deposit_is_not_a_daily_loss(maker, qs):
    """A REVERSAL of a deposit (or withdrawal) is an external cash flow too."""
    from smtrading.ledger.service import reverse_journal
    from smtrading.risk.daily_loss import net_cash_flows_eur

    async with maker() as s:
        pid = (await _portfolio(s)).id
        await s.commit()
    await _day_open_now(maker, qs, pid)
    async with maker() as s:
        dep = await deposit(s, pid, D(20000), "EUR", author_id="admin")
        wd = await withdraw(s, pid, D(5000), "EUR", author_id="admin")
        await s.commit()
    async with maker() as s:
        await reverse_journal(s, dep.id, reason="errore", author_type="admin", author_id="a")
        await reverse_journal(s, wd.id, reason="errore", author_type="admin", author_id="a")
        await s.commit()
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        now = datetime.now(UTC)
        day_open = await ensure_day_open(s, qs, p, now)
        assert await net_cash_flows_eur(s, qs, pid, day_open.ts, now) == D(0)
        assert await check_daily_loss(s, qs, p, now) is False
        assert p.status == PortfolioStatus.active


async def test_reset_daily_locks_rows_then_updates(db_session, seeded):
    from smtrading.risk.daily_loss import DAILY_LOSS_REASON

    ps = [await _portfolio(db_session) for _ in range(3)]
    past = datetime(2026, 11, 10, tzinfo=UTC)
    for p, until in zip(ps, (past, past, past + timedelta(days=5)), strict=True):
        p.status = PortfolioStatus.reduce_only
        p.reduce_only_reason = DAILY_LOSS_REASON
        p.reduce_only_until = until
    await db_session.flush()
    assert await reset_daily(db_session, past + timedelta(hours=1)) == 2
    for p in ps:
        await db_session.refresh(p)
    assert [p.status for p in ps] == [
        PortfolioStatus.active, PortfolioStatus.active, PortfolioStatus.reduce_only,
    ]
    assert await reset_daily(db_session, past + timedelta(hours=1)) == 0


async def test_take_snapshots_logs_provider_errors_without_traceback(maker, qs, caplog):
    async with maker() as s:
        p = await _portfolio(s)
        await _buy(s, qs, p.id, 10)
        await s.commit()
    del qs._quotes["ENI.MI"]  # the valuation cannot mark the position
    with caplog.at_level("WARNING", logger="smtrading.corporate.snapshots"):
        await take_snapshots(maker, qs, clock=lambda: NOW)
    [record] = [r for r in caplog.records if r.name == "smtrading.corporate.snapshots"]
    assert record.levelname == "WARNING" and record.exc_info is None
    assert "PROVIDER_UNAVAILABLE" in record.getMessage()
