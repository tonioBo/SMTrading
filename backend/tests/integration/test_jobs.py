import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.corporate.actions import run_corporate_actions
from smtrading.corporate.interest import run_interest
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.jobs.runs import run_job_once
from smtrading.jobs.scheduler import JobScheduler
from smtrading.ledger.models import AccountKind, Journal, JournalLine, JournalType
from smtrading.ledger.service import (
    LineSpec,
    get_balance,
    get_or_create_account,
    post_journal,
    verify_chain,
)
from smtrading.marketdata.types import CalendarEvent
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.models import EquitySnapshot, JobRun
from smtrading.seed import run_seed

D = Decimal
DAY = date(2026, 11, 12)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D(200), D(200), D(200))
    return q


async def _portfolio(session, ccy="USD", cash=100000):
    p = await create_portfolio(
        session, name=f"J-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), base_currency=ccy,
    )
    return p.id  # ids survive the jobs' commits (ORM objects expire)


async def _trade(session, qs, portfolio_id, side, qty, symbol="AAPL"):
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=side, type=OrderType.market,
        qty=D(qty), status=OrderStatus.new,
    )
    session.add(o)
    await session.flush()
    quote = (await qs.get_quotes([symbol]))[symbol]
    assert await execute_fill(session, qs, o.id, quote=quote) is not None


async def _balance(session, portfolio_id, asset, kind):
    acct = await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    return await get_balance(session, acct.id)


async def _journals(session, type_):
    return await session.scalar(select(func.count()).select_from(Journal).where(Journal.type == type_))


async def _borrow_eur(session, portfolio_id, amount):
    loan = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset="EUR", kind=AccountKind.margin_loan
    )
    cash = await get_or_create_account(
        session, portfolio_id=portfolio_id, asset="EUR", kind=AccountKind.cash
    )
    await post_journal(
        session, type=JournalType.TRADE, author_type="system", author_id=None,
        lines=[LineSpec(loan.id, D(amount).copy_negate()), LineSpec(cash.id, D(amount))],
    )


async def test_run_job_once_claims_a_key_once(db_session):
    assert await run_job_once(db_session, "x", "k") is True
    assert await run_job_once(db_session, "x", "k") is False
    assert await run_job_once(db_session, "x", "k2") is True


async def test_margin_loan_interest_from_cash_then_idempotent(db_session, seeded, qs):
    pid = await _portfolio(db_session, "EUR", 0)
    await _borrow_eur(db_session, pid, 36000)

    assert await run_interest(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "EUR", AccountKind.cash) == D("35995.00")
    assert await _balance(db_session, pid, "EUR", AccountKind.margin_loan) == D(-36000)
    system = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_interest
    )
    assert await get_balance(db_session, system.id) == D("5.00")
    journal = await db_session.scalar(select(Journal).where(Journal.type == JournalType.INTEREST))
    assert journal is not None

    assert await run_interest(db_session, qs, DAY) == 0
    assert await _journals(db_session, JournalType.INTEREST) == 1
    assert await db_session.get(JobRun, ("interest", DAY.isoformat())) is not None
    assert (await verify_chain(db_session)).ok


async def test_margin_loan_interest_without_cash_grows_the_loan(db_session, seeded, qs):
    pid = await _portfolio(db_session, "EUR", 0)
    await _borrow_eur(db_session, pid, 36000)
    cash = await get_or_create_account(
        db_session, portfolio_id=pid, asset="EUR", kind=AccountKind.cash
    )
    sink = await get_or_create_account(
        db_session, portfolio_id=None, asset="EUR", kind=AccountKind.system_equity
    )
    await post_journal(  # spend the borrowed cash
        db_session, type=JournalType.WITHDRAWAL, author_type="admin", author_id=None,
        lines=[LineSpec(cash.id, D(-36000)), LineSpec(sink.id, D(36000))],
    )
    assert await run_interest(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "EUR", AccountKind.cash) == 0
    assert await _balance(db_session, pid, "EUR", AccountKind.margin_loan) == D("-36005.00")


async def test_short_borrow_fee_from_cash(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.sell, 5)
    cash_before = await _balance(db_session, pid, "USD", AccountKind.cash)

    assert await run_interest(db_session, qs, DAY) == 1  # 1000 x 3/100/360 = 0.0833
    assert await _balance(db_session, pid, "USD", AccountKind.cash) == cash_before - D("0.08")
    system = await get_or_create_account(
        db_session, portfolio_id=None, asset="USD", kind=AccountKind.system_interest
    )
    assert await get_balance(db_session, system.id) == D("0.08")


async def test_short_borrow_fee_without_cash_goes_on_margin_loan(db_session, seeded, qs):
    pid = await _portfolio(db_session, "EUR")  # no USD cash: the fee is borrowed
    await _trade(db_session, qs, pid, OrderSide.sell, 5)
    assert await _balance(db_session, pid, "USD", AccountKind.cash) == 0

    assert await run_interest(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "USD", AccountKind.margin_loan) == D("-0.08")
    assert await _balance(db_session, pid, "USD", AccountKind.cash) == 0


async def test_no_interest_without_loan_or_short(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.buy, 5)
    assert await run_interest(db_session, qs, DAY) == 0
    assert await _journals(db_session, JournalType.INTEREST) == 0


async def test_interest_portfolio_without_quote_is_retried(db_session, seeded, qs, caplog):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.sell, 5)
    empty = FakeQuoteSource()
    with caplog.at_level(logging.ERROR, logger="smtrading.corporate.interest"):
        assert await run_interest(db_session, empty, DAY) == 0
    assert any("interest" in r.getMessage() for r in caplog.records)
    assert await db_session.get(JobRun, ("interest", DAY.isoformat())) is None
    assert await run_interest(db_session, qs, DAY) == 1  # retried: not claimed


async def test_dividend_long_and_short(db_session, seeded, qs):
    long_id = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, long_id, OrderSide.buy, 10)
    short_id = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, short_id, OrderSide.sell, 10)
    long_cash = await _balance(db_session, long_id, "USD", AccountKind.cash)
    short_cash = await _balance(db_session, short_id, "USD", AccountKind.cash)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25", "currency": "USD"})])

    assert await run_corporate_actions(db_session, qs, DAY) == 2
    assert await _balance(db_session, long_id, "USD", AccountKind.cash) == long_cash + D("2.50")
    assert await _balance(db_session, short_id, "USD", AccountKind.cash) == short_cash - D("2.50")
    assert await _journals(db_session, JournalType.DIVIDEND) == 2

    assert await run_corporate_actions(db_session, qs, DAY) == 0  # idempotent
    assert await _journals(db_session, JournalType.DIVIDEND) == 2
    assert (await verify_chain(db_session)).ok


async def test_dividend_currency_defaults_to_instrument(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.buy, 10)
    cash = await _balance(db_session, pid, "USD", AccountKind.cash)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25", "currency": None})])
    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "USD", AccountKind.cash) == cash + D("2.50")


async def test_short_dividend_without_cash_goes_on_margin_loan(db_session, seeded, qs):
    pid = await _portfolio(db_session, "EUR")
    await _trade(db_session, qs, pid, OrderSide.sell, 10)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25"})])
    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "USD", AccountKind.margin_loan) == D("-2.50")


async def test_split_multiplies_qty_and_divides_avg(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.buy, 10)
    qs.set_calendar([CalendarEvent("AAPL", "split", DAY, {"ratio": "4"})])

    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(40)
    pos = await db_session.get(Position, (pid, "AAPL"), populate_existing=True)
    assert (pos.qty, pos.avg_price) == (D(40), D(50))
    journal = await db_session.scalar(
        select(Journal).where(Journal.type == JournalType.CORPORATE_ACTION)
    )
    lines = sorted(
        (await db_session.scalars(
            select(JournalLine.quantity).where(JournalLine.journal_id == journal.id)
        )).all()
    )
    assert lines == [D(-30), D(30)]

    assert await run_corporate_actions(db_session, qs, DAY) == 0
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(40)
    assert (await verify_chain(db_session)).ok


async def test_split_on_a_short(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.sell, 10)
    qs.set_calendar([CalendarEvent("AAPL", "split", DAY, {"ratio": "4"})])
    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(-40)
    pos = await db_session.get(Position, (pid, "AAPL"), populate_existing=True)
    assert (pos.qty, pos.avg_price) == (D(-40), D(50))


async def test_events_of_other_days_and_symbols_are_ignored(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.buy, 10)
    qs.set_calendar([
        CalendarEvent("AAPL", "dividend", DAY - timedelta(days=1), {"amount": "1"}),
        CalendarEvent("MSFT", "dividend", DAY, {"amount": "1"}),
        CalendarEvent("AAPL", "earnings", DAY, {}),
    ])
    assert await run_corporate_actions(db_session, qs, DAY) == 0


# --- scheduler -----------------------------------------------------------------------


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
async def sm(fresh_db):
    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await run_seed(session)
        await session.commit()
    yield maker
    await engine.dispose()


async def test_scheduler_runs_daily_and_periodic_jobs(sm, qs, monkeypatch):
    async with sm() as session:
        pid = await _portfolio(session, "EUR", 0)
        await _borrow_eur(session, pid, 36000)
        await session.commit()
    qs.set_calendar([])
    sweeps: list[datetime] = []

    async def fake_sweep(sessionmaker, qs_, redis, *, clock):
        sweeps.append(clock())

    monkeypatch.setattr("smtrading.jobs.scheduler.margin_sweep", fake_sweep)
    clock = _Clock(datetime(2026, 11, 13, 0, 0, 10, tzinfo=UTC))
    scheduler = JobScheduler(sm, qs, None, clock=clock)

    await scheduler.tick()  # 00:00:10: interest of the 12th, no corporate (before 06:00)
    async with sm() as session:
        assert await _journals(session, JournalType.INTEREST) == 1
        assert await session.get(JobRun, ("corporate", "2026-11-13")) is None
        snaps = await session.scalar(
            select(func.count()).select_from(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid)
        )
        assert snaps == 2  # day_open + periodic (the seed's demo portfolio has its own)
    assert len(sweeps) == 1

    clock.now += timedelta(seconds=30)
    await scheduler.tick()  # nothing due yet
    assert len(sweeps) == 1
    clock.now += timedelta(seconds=30)
    await scheduler.tick()  # 60s: margin sweep again, interest not repeated
    assert len(sweeps) == 2
    async with sm() as session:
        assert await _journals(session, JournalType.INTEREST) == 1
        snaps = await session.scalar(
            select(func.count()).select_from(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid)
        )
        assert snaps == 2

    clock.now = datetime(2026, 11, 13, 6, 0, 5, tzinfo=UTC)
    await scheduler.tick()
    async with sm() as session:
        assert await session.get(JobRun, ("corporate", "2026-11-13")) is not None
        snaps = await session.scalar(
            select(func.count()).select_from(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid)
        )
        assert snaps == 3


async def test_scheduler_logs_errors_and_keeps_going(sm, qs, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("smtrading.jobs.scheduler.margin_sweep", boom)
    monkeypatch.setattr("smtrading.jobs.scheduler.run_interest", boom)
    clock = _Clock(datetime(2026, 11, 13, 7, 0, tzinfo=UTC))
    scheduler = JobScheduler(sm, qs, None, clock=clock)
    with caplog.at_level(logging.ERROR, logger="smtrading.jobs.scheduler"):
        await scheduler.tick()
    messages = [r.getMessage() for r in caplog.records]
    assert any("margin sweep" in m for m in messages)
    assert any("interest" in m for m in messages)
    async with sm() as session:  # the other jobs still ran
        assert await session.get(JobRun, ("corporate", "2026-11-13")) is not None


# --- I5 / Ruling R21: splits cancel open orders ---------------------------------------


async def _open_order(session, portfolio_id, symbol="AAPL", side=OrderSide.sell):
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=side, type=OrderType.limit,
        qty=D(5), limit_price=D(300), status=OrderStatus.pending,
    )
    session.add(o)
    await session.flush()
    return o.id


async def _notification_kinds(session, portfolio_id):
    from smtrading.risk.models import Notification

    return list(await session.scalars(
        select(Notification.kind).where(Notification.portfolio_id == portfolio_id)
    ))


async def test_split_cancels_open_orders_in_every_portfolio(db_session, seeded, qs):
    holder = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, holder, OrderSide.buy, 10)
    held_order = await _open_order(db_session, holder)
    other = await _portfolio(db_session, "USD")  # no position, only an open order
    other_order = await _open_order(db_session, other, side=OrderSide.buy)
    unrelated = await _open_order(db_session, other, symbol="ENI.MI", side=OrderSide.buy)
    qs.set_calendar([CalendarEvent("AAPL", "split", DAY, {"ratio": "4"})])

    assert await run_corporate_actions(db_session, qs, DAY) == 1

    assert await _balance(db_session, holder, "AAPL", AccountKind.position) == D(40)
    statuses = {
        oid: (await db_session.get(Order, oid, populate_existing=True)).status
        for oid in (held_order, other_order, unrelated)
    }
    assert statuses == {
        held_order: OrderStatus.canceled, other_order: OrderStatus.canceled,
        unrelated: OrderStatus.pending,
    }
    assert await _notification_kinds(db_session, holder) == ["split_orders_canceled"]
    assert await _notification_kinds(db_session, other) == ["split_orders_canceled"]


async def test_split_of_a_symbol_only_in_open_orders(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    oid = await _open_order(db_session, pid, side=OrderSide.buy)
    qs.set_calendar([CalendarEvent("AAPL", "split", DAY, {"ratio": "2"})])
    assert await run_corporate_actions(db_session, qs, DAY) == 0  # no journal
    assert (await db_session.get(Order, oid, populate_existing=True)).status == (
        OrderStatus.canceled
    )


async def test_dividend_does_not_cancel_open_orders(db_session, seeded, qs):
    pid = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, pid, OrderSide.buy, 10)
    oid = await _open_order(db_session, pid)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25"})])
    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert (await db_session.get(Order, oid, populate_existing=True)).status == (
        OrderStatus.pending
    )


# --- I6: catch-up of missed days --------------------------------------------------------


async def test_scheduler_catches_up_missed_days_in_order(sm, qs, monkeypatch, caplog):
    async with sm() as session:
        for job, day in (("interest", "2026-11-06"), ("interest", "2026-11-07"),
                         ("interest", "2026-11-08:00000000-0000-0000-0000-000000000000"),
                         ("corporate", "2026-11-10")):
            session.add(JobRun(job=job, key=day))
        await session.commit()
    ran: list[tuple[str, date]] = []

    def fake(job):
        async def run(session, qs_, day, **kw):
            ran.append((job, day))
            await run_job_once(session, job, day.isoformat())
            await session.commit()
            return 0
        return run

    monkeypatch.setattr("smtrading.jobs.scheduler.run_interest", fake("interest"))
    monkeypatch.setattr("smtrading.jobs.scheduler.run_corporate_actions", fake("corporate"))

    async def no_sweep(*args, **kwargs):
        pass

    monkeypatch.setattr("smtrading.jobs.scheduler.margin_sweep", no_sweep)
    clock = _Clock(datetime(2026, 11, 13, 0, 0, 10, tzinfo=UTC))  # corporate not due yet
    scheduler = JobScheduler(sm, qs, None, clock=clock)
    with caplog.at_level(logging.ERROR, logger="smtrading.jobs.scheduler"):
        await scheduler.tick()

    days = [date(2026, 11, d) for d in range(8, 13)]
    assert ran == [("interest", d) for d in days] + [
        ("corporate", date(2026, 11, 11)), ("corporate", date(2026, 11, 12)),
    ]
    caught = sorted(r.getMessage() for r in caplog.records if "catching up" in r.getMessage())
    assert caught == sorted(
        [f"interest {d.isoformat()} was missed: catching up" for d in days[:-1]]
        + ["corporate 2026-11-11 was missed: catching up",
           "corporate 2026-11-12 was missed: catching up"]
    )
    assert all(r.levelname == "ERROR" for r in caplog.records if "catching up" in r.getMessage())

    ran.clear()
    clock.now += timedelta(seconds=30)
    await scheduler.tick()
    assert ran == []  # everything done
    clock.now = datetime(2026, 11, 13, 6, 0, 5, tzinfo=UTC)
    await scheduler.tick()
    assert ran == [("corporate", date(2026, 11, 13))]


async def test_scheduler_does_not_catch_up_a_job_that_never_ran(sm, qs, monkeypatch):
    ran: list[date] = []

    async def run(session, qs_, day, **kw):
        ran.append(day)
        return 0

    monkeypatch.setattr("smtrading.jobs.scheduler.run_interest", run)
    scheduler = JobScheduler(sm, qs, None, clock=_Clock(datetime(2026, 11, 13, 1, tzinfo=UTC)))
    await scheduler._daily("interest", time(0), datetime(2026, 11, 13, 1, tzinfo=UTC),
                           date(2026, 11, 12), run)
    assert ran == [date(2026, 11, 12)]


async def test_corporate_actions_skip_real_money_portfolios(db_session, seeded, qs):
    """R8.6 (plan 8): a portfolio linked to a real-money link follows the broker: no local
    dividend, split or split cancel; a paper portfolio still gets them."""
    from smtrading.brokers.models import BrokerLink
    from smtrading.portfolios.models import Portfolio

    paper = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, paper, OrderSide.buy, 10)
    live = await _portfolio(db_session, "USD")
    await _trade(db_session, qs, live, OrderSide.buy, 10)
    live_order = await _open_order(db_session, live)
    link = BrokerLink(name=f"R-{uuid.uuid4().hex[:6]}", kind="alpaca_live", live_state="armed")
    db_session.add(link)
    await db_session.flush()
    (await db_session.get(Portfolio, live)).broker_link_id = link.id
    await db_session.flush()
    live_cash = await _balance(db_session, live, "USD", AccountKind.cash)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25", "currency": "USD"}),
                     CalendarEvent("AAPL", "split", DAY, {"ratio": "4"})])

    assert await run_corporate_actions(db_session, qs, DAY) == 2  # the paper one only
    assert await _balance(db_session, paper, "AAPL", AccountKind.position) == D(40)
    assert await _balance(db_session, live, "AAPL", AccountKind.position) == D(10)
    assert await _balance(db_session, live, "USD", AccountKind.cash) == live_cash
    assert (await db_session.get(Order, live_order, populate_existing=True)).status \
        == OrderStatus.pending
    assert await _notification_kinds(db_session, live) == []
