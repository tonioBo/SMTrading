import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import discard_post_commit
from smtrading.ledger.models import Account, AccountKind, Journal, JournalLine, JournalType
from smtrading.ledger.service import get_balance, get_or_create_account
from smtrading.portfolios.service import create_portfolio, deposit
from smtrading.positions.models import Position
from smtrading.risk.models import RiskProfile
from smtrading.seed import run_seed

D = Decimal


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("200.00"), D("200.10"), D("200.05"))
    q.set_quote("MSFT", D("400.00"), D("400.10"), D("400.05"))
    q.set_quote("ENI.MI", D("14.990"), D("15.000"), D("14.995"))
    q.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    return q


async def _portfolio(session, leverage=1, cash=100000):
    return await create_portfolio(
        session, name=f"S-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), leverage=D(leverage),
    )


async def _order(session, portfolio, symbol, side, qty, *, type=OrderType.market):
    o = Order(
        portfolio_id=portfolio.id, symbol=symbol, side=side, type=type, qty=qty,
        status=OrderStatus.new,
    )
    session.add(o)
    await session.flush()
    return o


async def _fill(session, qs, portfolio, symbol, side, qty):
    o = await _order(session, portfolio, symbol, side, qty)
    quote = (await qs.get_quotes([symbol]))[symbol]
    return await execute_fill(session, qs, o.id, quote=quote)


async def _bal(session, portfolio_id, asset, kind):
    acct = await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    return await get_balance(session, acct.id)


async def _lines(session, journal_id):
    rows = await session.execute(
        select(Account.kind, Account.asset, Account.portfolio_id, JournalLine.quantity)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.journal_id == journal_id)
    )
    return sorted((str(k), a, p is not None, q) for k, a, p, q in rows.all())


async def _position(session, portfolio_id, symbol):
    return await session.scalar(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol == symbol)
    )


async def test_short_sell_creates_collateral(db_session, seeded, qs):
    p = await _portfolio(db_session)
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))

    assert [j.type for j in res.journals] == [JournalType.TRADE]
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "AAPL", True, D(-5)),
        ("system_equity", "AAPL", False, D(5)),
        ("collateral", "USD", True, D("1000.00")),
        ("system_equity", "USD", False, D("-1000.00")),
        ("cash", "EUR", True, D("-1.80")),
        ("system_fees", "EUR", False, D("1.80")),
    ])
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(-5)
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("1000.00")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("99998.20")
    assert (res.fill.fee_amount, res.fill.fee_currency) == (D("1.80"), "EUR")
    pos = await _position(db_session, p.id, "AAPL")
    assert pos.qty == D(-5) and pos.avg_price == D(200)


async def test_short_not_allowed_profile(db_session, seeded, qs):
    profile = RiskProfile(name=f"no-short-{uuid.uuid4().hex[:8]}", short_allowed=False)
    db_session.add(profile)
    await db_session.flush()
    p = await _portfolio(db_session)
    p.risk_profile_id = profile.id
    await db_session.flush()
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED


async def test_short_not_allowed_instrument(db_session, seeded, qs):
    p = await _portfolio(db_session)
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, p, "BTC-USD", OrderSide.sell, D("0.1"))
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED


async def test_short_fee_without_cash_leverage_1(db_session, seeded, qs):
    p = await _portfolio(db_session, cash=D(0))
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER


async def test_short_blocked_by_buying_power(db_session, seeded, qs):
    # leverage 1: bp = equity - exposure ~= cash - short value; 600 EUR cannot carry 800 EUR
    p = await _portfolio(db_session, cash=D(600))
    with pytest.raises(SMTError) as e:
        async with db_session.begin_nested():
            await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    discard_post_commit(db_session)
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(0)


async def test_cover_uses_collateral_first(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    qs.set_quote("AAPL", D("179.90"), D("180.00"), D("179.95"))
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(5))

    assert [j.type for j in res.journals] == [JournalType.TRADE]
    # 900 USD paid out of collateral (1000 - 900 = 100 left); the position is flat and no
    # other USD short remains, so the 100 USD left over is released to cash.
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "AAPL", True, D(5)),
        ("system_equity", "AAPL", False, D(-5)),
        ("collateral", "USD", True, D("-1000.00")),
        ("cash", "USD", True, D("1000.00")),
        ("cash", "USD", True, D("-900.00")),
        ("system_equity", "USD", False, D("900.00")),
        ("cash", "EUR", True, D("-1.72")),
        ("system_fees", "EUR", False, D("1.72")),
    ])
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(0)
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D(0)
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D("100.00")
    pos = await _position(db_session, p.id, "AAPL")
    assert pos.qty == D(0) and pos.realized_pnl == D(100)


async def test_partial_cover_keeps_rest_in_collateral(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    qs.set_quote("AAPL", D("179.90"), D("180.00"), D("179.95"))
    await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(2))
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("640.00")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(-3)


async def test_cover_keeps_collateral_of_other_shorts(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))  # 1000 USD
    await _fill(db_session, qs, p, "MSFT", OrderSide.sell, D(1))  # 400 USD
    qs.set_quote("AAPL", D("179.90"), D("180.00"), D("179.95"))
    await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(5))
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("500.00")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)


async def test_cover_at_loss_pays_from_cash_after_collateral(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    qs.set_quote("AAPL", D("219.90"), D("220.00"), D("219.95"))
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(5))
    fx, _ = res.journals
    assert fx.type == JournalType.FX
    # 1100 USD: 1000 from collateral, the 100 USD shortfall bought with 80 EUR
    assert ("cash", "USD", True, D("100.00")) in await _lines(db_session, fx.id)
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D(0)
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    pos = await _position(db_session, p.id, "AAPL")
    assert pos.qty == D(0) and pos.realized_pnl == D(-100)


async def test_cover_beyond_short_opens_long(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(5))
    qs.set_quote("AAPL", D("179.90"), D("180.00"), D("179.95"))
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(8))
    trade = res.journals[-1]
    assert trade.type == JournalType.TRADE
    assert sum(1 for j in res.journals if j.type == JournalType.TRADE) == 1
    # 8 x 180 = 1440 USD: 1000 from collateral, 440 bought via FX
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(3)
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D(0)
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    pos = await _position(db_session, p.id, "AAPL")
    assert pos.qty == D(3) and pos.avg_price == D(180) and pos.realized_pnl == D(100)


async def test_flip_long_to_short(db_session, seeded, qs):
    p = await _portfolio(db_session)
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(10))  # avg 200
    qs.set_quote("AAPL", D("210.00"), D("210.10"), D("210.05"))
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(15))

    assert [j.type for j in res.journals] == [JournalType.TRADE]
    # 15 x 210 = 3150: 2100 for the closed long go to cash, 1050 for the short to collateral
    # fee: 3150 USD = 2520 EUR -> 1 + 2.52 = 3.52 EUR
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "AAPL", True, D(-15)),
        ("system_equity", "AAPL", False, D(15)),
        ("cash", "USD", True, D("2100.00")),
        ("system_equity", "USD", False, D("-2100.00")),
        ("collateral", "USD", True, D("1050.00")),
        ("system_equity", "USD", False, D("-1050.00")),
        ("cash", "EUR", True, D("-3.52")),
        ("system_fees", "EUR", False, D("3.52")),
    ])
    assert await _bal(db_session, p.id, "AAPL", AccountKind.position) == D(-5)
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D("2100.00")
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("1050.00")
    pos = await _position(db_session, p.id, "AAPL")
    assert pos.qty == D(-5) and pos.avg_price == D(210) and pos.realized_pnl == D(100)


async def test_reducing_sell_fee_from_proceeds_when_no_fee_cash(db_session, seeded, qs):
    """R9: 10 AAPL at ask 200 cost 1600 EUR + 4 fx fee + 2.60 fee, leaving 0 EUR."""
    p = await _portfolio(db_session, cash=D("1606.60"))
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(10))
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)

    res = await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(10))
    # proceeds 1999.00 USD; fee 1 + 0.1% x 1599.20 EUR = 2.60 EUR = 3.25 USD
    assert (res.fill.fee_amount, res.fill.fee_currency) == (D("3.25"), "USD")
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "AAPL", True, D(-10)),
        ("system_equity", "AAPL", False, D(10)),
        ("cash", "USD", True, D("1999.00")),
        ("system_equity", "USD", False, D("-1999.00")),
        ("cash", "USD", True, D("-3.25")),
        ("system_fees", "USD", False, D("3.25")),
    ])
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D("1995.75")
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)


async def test_reducing_cover_fee_from_collateral_when_no_fee_cash(db_session, seeded, qs):
    # short 2 AAPL @200: fee 1 + 0.1% x 320 EUR = 1.32 EUR leaves 0 EUR
    p = await _portfolio(db_session, cash=D("1.32"))
    await deposit(db_session, p.id, D(500), "USD", author_id=None, reason="usd")
    qs.set_quote("AAPL", D("200.00"), D("200.00"), D("200.00"))
    await _fill(db_session, qs, p, "AAPL", OrderSide.sell, D(2))
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("400.00")

    qs.set_quote("AAPL", D("180.00"), D("180.00"), D("180.00"))
    res = await _fill(db_session, qs, p, "AAPL", OrderSide.buy, D(1))
    # fee 1 + 0.1% x 144 EUR = 1.14 EUR, no EUR cash -> 1.425 USD rounded up to 1.43
    assert (res.fill.fee_amount, res.fill.fee_currency) == (D("1.43"), "USD")
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "AAPL", True, D(1)),
        ("system_equity", "AAPL", False, D(-1)),
        ("collateral", "USD", True, D("-181.43")),
        ("cash", "USD", True, D("181.43")),
        ("cash", "USD", True, D("-180.00")),
        ("system_equity", "USD", False, D("180.00")),
        ("cash", "USD", True, D("-1.43")),
        ("system_fees", "USD", False, D("1.43")),
    ])
    # cover cost and fee both come out of collateral first
    assert await _bal(db_session, p.id, "USD", AccountKind.collateral) == D("218.57")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D("500.00")
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)


async def test_reducing_fill_never_blocked_by_margin(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=2)
    await _fill(db_session, qs, p, "ENI.MI", OrderSide.buy, D(10000))  # borrows 50020
    qs.set_quote("ENI.MI", D("9.000"), D("9.010"), D("9.005"))  # equity now deeply short
    res = await _fill(db_session, qs, p, "ENI.MI", OrderSide.sell, D(1000))
    assert res is not None
    assert await _bal(db_session, p.id, "ENI.MI", AccountKind.position) == D(9000)


async def _setup_committed(url):
    engine = make_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await run_seed(s)
            p = await _portfolio(s)
            o = await _order(s, p, "ENI.MI", OrderSide.buy, D(10))
            await s.commit()
            return o.id
    finally:
        await engine.dispose()


async def test_concurrent_execute_single_fill(fresh_db, qs):
    order_id = await _setup_committed(fresh_db)
    quote = (await qs.get_quotes(["ENI.MI"]))["ENI.MI"]
    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    start = asyncio.Event()

    async def worker():
        async with maker() as s:
            await start.wait()
            res = await execute_fill(s, qs, order_id, quote=quote)
            await s.commit()
            return res

    try:
        tasks = [asyncio.create_task(worker()) for _ in range(2)]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.gather(*tasks)
        assert sum(r is not None for r in results) == 1
        async with maker() as s:
            fills = await s.scalar(
                select(func.count()).select_from(Fill).where(Fill.order_id == order_id)
            )
            trades = await s.scalar(
                select(func.count()).select_from(Journal)
                .where(Journal.order_id == order_id, Journal.type == JournalType.TRADE)
            )
            status = await s.scalar(select(Order.status).where(Order.id == order_id))
        assert (fills, trades, status) == (1, 1, OrderStatus.filled)
    finally:
        await engine.dispose()
