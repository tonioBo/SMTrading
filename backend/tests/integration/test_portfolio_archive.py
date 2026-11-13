"""Task 13: archived portfolios: hidden, read-only and skipped by every job."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.admin.portfolio_ops import archive_portfolio, unarchive_portfolio
from smtrading.agents import service as agents
from smtrading.agents.models import AgentPortfolio, McpAuditLog
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.corporate.actions import run_corporate_actions
from smtrading.corporate.interest import run_interest
from smtrading.corporate.snapshots import take_snapshots
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    cancel_all_orders,
    close_all_positions,
    close_position,
    modify_order,
    place_order,
    set_kill_switch_and_cancel,
)
from smtrading.jobs.scheduler import JobScheduler
from smtrading.ledger.models import AccountKind, Journal, JournalType
from smtrading.ledger.service import (
    LineSpec,
    get_balance,
    get_or_create_account,
    post_journal,
)
from smtrading.marketdata.types import CalendarEvent
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.margin import check_portfolio_margin, margin_holdings, margin_sweep
from smtrading.risk.models import EquitySnapshot
from smtrading.seed import run_seed

D = Decimal
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
TUE = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)  # Tuesday 11:00 Rome: MTA open
DAY = date(2026, 11, 12)


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None)
    return q


async def _portfolio(session, *, ccy="EUR", cash=100000, leverage=1) -> uuid.UUID:
    p = await create_portfolio(
        session, name=f"A-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), base_currency=ccy,
        leverage=D(leverage),
    )
    return p.id


async def _fill(session, qs, portfolio_id, symbol, side, qty):
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=side, type=OrderType.market,
        qty=D(qty), status=OrderStatus.new,
    )
    session.add(o)
    await session.flush()
    quote = (await qs.get_quotes([symbol]))[symbol]
    assert await execute_fill(session, qs, o.id, quote=quote) is not None


async def _resting(session, portfolio_id, symbol="AAPL") -> Order:
    o = Order(
        portfolio_id=portfolio_id, symbol=symbol, side=OrderSide.buy, type=OrderType.limit,
        qty=D(1), limit_price=D(100), status=OrderStatus.pending,
    )
    session.add(o)
    await session.flush()
    return o


async def _force_archived(session, portfolio_id) -> None:
    """Archived bypassing the service checks (e.g. with an order left behind)."""
    p = await session.get(Portfolio, portfolio_id)
    p.status = PortfolioStatus.archived
    await session.flush()


async def _balance(session, portfolio_id, asset, kind):
    acct = await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    return await get_balance(session, acct.id)


def _code(excinfo) -> str:
    return str(excinfo.value.code)


# --- archive / unarchive -------------------------------------------------------------


async def test_archive_and_unarchive_round_trip(db_session, seeded):
    pid = await _portfolio(db_session)
    p = await archive_portfolio(db_session, pid, by="admin@example.com")
    assert p.status == PortfolioStatus.archived
    assert p.archived_by == "admin@example.com" and p.archived_at is not None
    assert p.archived_prior_status == "active"
    again = await archive_portfolio(db_session, pid, by="other@example.com")  # idempotent
    assert again.archived_by == "admin@example.com"

    p = await unarchive_portfolio(db_session, pid, qs=FakeQuoteSource(), by="admin@example.com")
    assert p.status == PortfolioStatus.active
    assert p.archived_at is None and p.archived_by is None and p.archived_prior_status is None
    assert (await unarchive_portfolio(db_session, pid, qs=FakeQuoteSource(), by="x")).status == PortfolioStatus.active

    methods = list(await db_session.scalars(
        select(McpAuditLog.method).where(McpAuditLog.params["portfolio_id"].astext == str(pid))
        .order_by(McpAuditLog.id)
    ))
    assert methods == ["admin:portfolio.archive", "admin:portfolio.unarchive"]


@pytest.mark.parametrize("prior", [PortfolioStatus.locked, PortfolioStatus.reduce_only])
async def test_unarchive_restores_the_prior_status(db_session, seeded, prior):
    pid = await _portfolio(db_session)
    p = await db_session.get(Portfolio, pid)
    p.status = prior
    p.reduce_only_reason = "daily_loss" if prior == PortfolioStatus.reduce_only else None
    await db_session.flush()
    await archive_portfolio(db_session, pid, by="a")
    p = await unarchive_portfolio(db_session, pid, qs=FakeQuoteSource(), by="a")
    assert p.status == prior
    if prior == PortfolioStatus.reduce_only:
        assert p.reduce_only_reason == "daily_loss"


async def test_archive_refused_with_open_orders(db_session, seeded):
    pid = await _portfolio(db_session)
    await _resting(db_session, pid)
    with pytest.raises(SMTError) as e:
        await archive_portfolio(db_session, pid, by="a")
    assert _code(e) == "INVALID_ORDER" and e.value.message == "Annulla prima gli ordini aperti"
    assert (await db_session.get(Portfolio, pid)).status == PortfolioStatus.active


async def test_archive_refused_with_a_broker_link(
    db_session, seeded, monkeypatch, pg_url, redis_url
):
    from smtrading.brokers.service import create_link, link_portfolio

    for k, v in {
        "SMT_DATABASE_URL": pg_url, "SMT_REDIS_URL": redis_url,
        "SMT_MASTER_KEY": "test-master-key", "SMT_ADMIN_EMAIL": "admin@example.com",
        "SMT_ADMIN_PASSWORD": "test-password",
    }.items():
        monkeypatch.setenv(k, v)
    pid = await _portfolio(db_session)
    link = await create_link(
        db_session, "L", "alpaca_paper", {"key_id": "K", "secret_key": "S"}
    )
    await link_portfolio(db_session, pid, link.id)
    with pytest.raises(SMTError) as e:
        await archive_portfolio(db_session, pid, by="a")
    assert _code(e) == "INVALID_ORDER" and e.value.message == "Scollega prima il broker"

    await link_portfolio(db_session, pid, None)
    await archive_portfolio(db_session, pid, by="a")
    with pytest.raises(SMTError) as e:  # an archived portfolio cannot be linked
        await link_portfolio(db_session, pid, link.id)
    assert _code(e) == "PORTFOLIO_ARCHIVED"


async def test_archive_keeps_positions_and_ledger(db_session, seeded, qs):
    pid = await _portfolio(db_session)
    await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    journals = await db_session.scalar(select(func.count()).select_from(Journal))
    await archive_portfolio(db_session, pid, by="a")
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(10)
    assert await db_session.scalar(select(func.count()).select_from(Journal)) == journals


# --- engine --------------------------------------------------------------------------


async def test_engine_refuses_an_archived_portfolio(db_session, seeded, qs):
    pid = await _portfolio(db_session)
    await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    order = await _resting(db_session, pid)
    await _force_archived(db_session, pid)
    orders_before = await db_session.scalar(select(func.count()).select_from(Order))

    req = PlaceOrderRequest(
        portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market, qty=D(1),
    )
    calls = [
        lambda: place_order(db_session, qs, req, now=OPEN),
        lambda: modify_order(db_session, qs, order.id, limit_price=D(101), now=OPEN),
        lambda: cancel_all_orders(db_session, pid, by="admin"),
        lambda: close_position(db_session, qs, pid, "AAPL", now=OPEN),
        lambda: close_all_positions(db_session, qs, pid, now=OPEN),
    ]
    for call in calls:
        with pytest.raises(SMTError) as e:
            await call()
        assert _code(e) == "PORTFOLIO_ARCHIVED" and e.value.message == "Portafoglio archiviato"
    # nothing persisted: no rejected order rows on an archived portfolio
    assert await db_session.scalar(select(func.count()).select_from(Order)) == orders_before

    fill = Order(
        portfolio_id=pid, symbol="AAPL", side=OrderSide.sell, type=OrderType.market,
        qty=D(1), status=OrderStatus.new,
    )
    db_session.add(fill)
    await db_session.flush()
    with pytest.raises(SMTError) as e:
        await execute_fill(db_session, qs, fill.id, quote=(await qs.get_quotes(["AAPL"]))["AAPL"])
    assert _code(e) == "PORTFOLIO_ARCHIVED"


async def test_kill_switch_cancel_skips_archived(db_session, seeded):
    live, archived = await _portfolio(db_session), await _portfolio(db_session)
    await _resting(db_session, live)
    left = await _resting(db_session, archived)
    await _force_archived(db_session, archived)
    assert await set_kill_switch_and_cancel(db_session, True, cancel_open=True) == 1
    assert (await db_session.get(Order, left.id)).status == OrderStatus.pending


# --- jobs ----------------------------------------------------------------------------


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


async def _journals(session, type_) -> int:
    return await session.scalar(
        select(func.count()).select_from(Journal).where(Journal.type == type_)
    )


async def test_interest_skips_archived(db_session, seeded, qs):
    live, archived = await _portfolio(db_session, cash=0), await _portfolio(db_session, cash=0)
    for pid in (live, archived):
        await _borrow_eur(db_session, pid, 36000)
    await _force_archived(db_session, archived)  # e.g. archived before the guard
    assert await run_interest(db_session, qs, DAY) == 1
    assert await _balance(db_session, archived, "EUR", AccountKind.cash) == D(36000)
    assert await _balance(db_session, live, "EUR", AccountKind.cash) == D("35995.00")


# --- unarchive catch-up / archive guardrails ------------------------------------------


async def _archived_days_ago(session, pid, days: int) -> date:
    await archive_portfolio(session, pid, by="a")
    p = await session.get(Portfolio, pid)
    p.archived_at = datetime.now(UTC) - timedelta(days=days)
    await session.flush()
    return datetime.now(UTC).date()


async def test_unarchive_applies_missed_split(db_session, seeded, qs):
    pid = await _portfolio(db_session, ccy="USD")
    await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    avg = (await db_session.get(Position, (pid, "AAPL"))).avg_price
    today = await _archived_days_ago(db_session, pid, 5)
    qs.set_calendar([
        CalendarEvent("AAPL", "split", today - timedelta(days=2), {"ratio": "2"}),
        CalendarEvent("AAPL", "split", today - timedelta(days=30), {"ratio": "3"}),  # before
    ])
    p = await unarchive_portfolio(db_session, pid, qs=qs, by="a")
    assert p.status == PortfolioStatus.active
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(20)
    pos = await db_session.get(Position, (pid, "AAPL"))
    assert pos.qty == D(20) and pos.avg_price == avg / 2

    await _archived_days_ago(db_session, pid, 5)  # second cycle: never applied twice
    await unarchive_portfolio(db_session, pid, qs=qs, by="a")
    assert await _balance(db_session, pid, "AAPL", AccountKind.position) == D(20)


async def test_unarchive_applies_missed_dividend(db_session, seeded, qs):
    pid = await _portfolio(db_session, ccy="USD")
    await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    before = await _balance(db_session, pid, "USD", AccountKind.cash)
    today = await _archived_days_ago(db_session, pid, 3)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", today - timedelta(days=1),
                                   {"amount": "0.25", "currency": "USD"})])
    await unarchive_portfolio(db_session, pid, qs=qs, by="a")
    assert await _balance(db_session, pid, "USD", AccountKind.cash) == before + D("2.50")


async def test_unarchive_refused_when_the_calendar_is_unavailable(db_session, seeded, qs):
    class NoCalendar(FakeQuoteSource):
        async def get_calendar(self, symbols, start, end):
            raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "no calendar provider")

    pid = await _portfolio(db_session, ccy="USD")
    await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    await _archived_days_ago(db_session, pid, 3)
    with pytest.raises(SMTError) as e:
        await unarchive_portfolio(db_session, pid, qs=NoCalendar(), by="a")
    assert _code(e) == "PROVIDER_UNAVAILABLE"
    assert e.value.message == (
        "Riprova: impossibile verificare gli eventi societari del periodo di archiviazione"
    )
    assert (await db_session.get(Portfolio, pid)).status == PortfolioStatus.archived


async def test_archive_refused_with_short_or_margin_loan(db_session, seeded, qs):
    short = await _portfolio(db_session, leverage=2)
    await _fill(db_session, qs, short, "ENI.MI", OrderSide.sell, 100)
    loan = await _portfolio(db_session, cash=0)
    await _borrow_eur(db_session, loan, 1000)
    for pid in (short, loan):
        with pytest.raises(SMTError) as e:
            await archive_portfolio(db_session, pid, by="a")
        assert _code(e) == "INVALID_ORDER"
        assert e.value.message == (
            "Chiudi prima le posizioni allo scoperto e rimborsa il prestito a margine"
        )
        assert (await db_session.get(Portfolio, pid)).status == PortfolioStatus.active


async def test_corporate_actions_skip_archived(db_session, seeded, qs):
    live = await _portfolio(db_session, ccy="USD")
    archived = await _portfolio(db_session, ccy="USD")
    for pid in (live, archived):
        await _fill(db_session, qs, pid, "AAPL", OrderSide.buy, 10)
    await archive_portfolio(db_session, archived, by="a")
    before = await _balance(db_session, archived, "USD", AccountKind.cash)
    qs.set_calendar([CalendarEvent("AAPL", "dividend", DAY, {"amount": "0.25", "currency": "USD"})])
    assert await run_corporate_actions(db_session, qs, DAY) == 1
    assert await _balance(db_session, archived, "USD", AccountKind.cash) == before


async def test_margin_skips_archived(db_session, seeded, qs):
    pid = await _portfolio(db_session, leverage=2)
    await _fill(db_session, qs, pid, "ENI.MI", OrderSide.buy, 13000)
    assert pid in (await margin_holdings(db_session))["ENI.MI"]
    await _force_archived(db_session, pid)  # e.g. archived before the guard
    assert "ENI.MI" not in await margin_holdings(db_session)
    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)  # deep in maintenance
    assert await check_portfolio_margin(db_session, qs, pid, now=TUE) == "ok"
    assert await _balance(db_session, pid, "ENI.MI", AccountKind.position) == D(13000)


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.aclose()


async def _snapshots(maker, pid) -> int:
    async with maker() as s:
        return await s.scalar(
            select(func.count()).select_from(EquitySnapshot)
            .where(EquitySnapshot.portfolio_id == pid)
        )


async def test_snapshots_and_sweeps_skip_archived(maker, redis, qs):
    async with maker() as s:
        live = await _portfolio(s, leverage=2)
        archived = await _portfolio(s, leverage=2)
        for pid in (live, archived):
            await _fill(s, qs, pid, "ENI.MI", OrderSide.buy, 13000)
        await _force_archived(s, archived)  # e.g. archived before the guard
        await s.commit()

    await take_snapshots(maker, qs, clock=lambda: TUE)
    assert await _snapshots(maker, live) == 2  # day_open + periodic
    assert await _snapshots(maker, archived) == 0

    scheduler = JobScheduler(maker, qs, None, clock=lambda: TUE)
    await scheduler._day_open()
    assert await _snapshots(maker, archived) == 0

    qs.set_quote("ENI.MI", D("8.995"), D("9.005"), None)  # maintenance: liquidation
    await margin_sweep(maker, qs, redis, clock=lambda: TUE)
    async with maker() as s:
        assert await _balance(s, live, "ENI.MI", AccountKind.position) == 0
        assert await _balance(s, archived, "ENI.MI", AccountKind.position) == D(13000)
        assert (await s.get(Portfolio, archived)).status == PortfolioStatus.archived


# --- agents --------------------------------------------------------------------------


async def test_agents_do_not_see_archived(db_session, seeded):
    live, archived = await _portfolio(db_session), await _portfolio(db_session)
    agent, _ = await agents.create_agent(db_session, f"ag-{uuid.uuid4().hex[:6]}")
    for pid in (live, archived):
        await agents.assign_portfolio(db_session, agent.id, pid, "trade")
    await archive_portfolio(db_session, archived, by="a")

    assert await agents.agent_portfolio_ids(db_session, agent.id) == [live]
    await agents.require_permission(db_session, agent.id, live, "trade")
    with pytest.raises(SMTError) as e:
        await agents.require_permission(db_session, agent.id, archived, "read")
    assert e.value.code == ErrorCode.NOT_FOUND
    kept = await db_session.scalar(
        select(AgentPortfolio.permission)
        .where(AgentPortfolio.agent_id == agent.id, AgentPortfolio.portfolio_id == archived)
    )
    assert kept == "trade"  # the assignment survives the archive
