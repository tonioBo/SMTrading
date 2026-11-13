import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.events import (
    POST_COMMIT_KEY,
    discard_post_commit,
    dispatch_post_commit,
    notify,
    publish_event,
    queue_event,
)
from smtrading.ledger.models import Account, AccountKind, Journal, JournalLine, JournalType
from smtrading.ledger.service import get_balance, get_or_create_account
from smtrading.marketdata.streaming import tracked_symbols
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.models import Notification
from smtrading.seed import run_seed

D = Decimal


@pytest.fixture
async def seeded(db_session):
    await run_seed(db_session)


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.990"), D("15.000"), D("14.995"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    return q


async def _portfolio(session, leverage=1):
    return await create_portfolio(
        session, name=f"X-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(leverage),
    )


async def _order(session, portfolio, symbol, side, *, qty=None, notional=None,
                 type=OrderType.market, status=OrderStatus.new, **kw):
    o = Order(
        portfolio_id=portfolio.id, symbol=symbol, side=side, type=type,
        qty=qty, notional=notional, status=status, **kw,
    )
    session.add(o)
    await session.flush()
    return o


async def _quote(qs, symbol):
    return (await qs.get_quotes([symbol]))[symbol]


async def _bal(session, portfolio_id, asset, kind):
    acct = await get_or_create_account(session, portfolio_id=portfolio_id, asset=asset, kind=kind)
    return await get_balance(session, acct.id)


async def _fill(session, qs, order, **kw):
    return await execute_fill(session, qs, order.id, quote=await _quote(qs, order.symbol), **kw)


async def _lines(session, journal_id):
    rows = await session.execute(
        select(Account.kind, Account.asset, Account.portfolio_id, JournalLine.quantity)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalLine.journal_id == journal_id)
    )
    return sorted((str(k), a, p is not None, q) for k, a, p, q in rows.all())


async def test_buy_eur_stock_cash(db_session, seeded, qs):
    p = await _portfolio(db_session)
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10))
    res = await _fill(db_session, qs, o)

    assert [j.type for j in res.journals] == [JournalType.TRADE]
    trade = res.journals[0]
    assert await _lines(db_session, trade.id) == sorted([
        ("position", "ENI.MI", True, D(10)),
        ("system_equity", "ENI.MI", False, D(-10)),
        ("cash", "EUR", True, D("-150.00")),
        ("system_equity", "EUR", False, D("150.00")),
        ("cash", "EUR", True, D("-1.15")),
        ("system_fees", "EUR", False, D("1.15")),
    ])
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("99848.85")
    pos = await db_session.scalar(select(Position).where(Position.portfolio_id == p.id))
    assert pos.qty == D(10) and pos.avg_price == D("15.000")

    assert res.order.status == OrderStatus.filled
    assert res.order.filled_qty == D(10) and res.order.avg_fill_price == D("15.000")
    f = res.fill
    assert (f.qty, f.price, f.fee_amount, f.fee_currency) == (D(10), D("15.000"), D("1.15"), "EUR")
    assert f.fee_rule_id is not None and f.journal_id == trade.id
    assert (f.quote_bid, f.quote_ask, f.quote_last) == (D("14.990"), D("15.000"), D("14.995"))
    assert f.quote_ts is not None
    assert trade.order_id == o.id and trade.fill_id == f.id


async def test_buy_usd_stock_with_fx(db_session, seeded, qs):
    p = await _portfolio(db_session)
    o = await _order(db_session, p, "AAPL", OrderSide.buy, qty=D(10))
    res = await _fill(db_session, qs, o)

    assert [j.type for j in res.journals] == [JournalType.FX, JournalType.TRADE]
    fx, trade = res.journals
    assert await _lines(db_session, fx.id) == sorted([
        ("cash", "EUR", True, D("-1600.00")),
        ("system_fx", "EUR", False, D("1600.00")),
        ("system_fx", "USD", False, D("-2000.00")),
        ("cash", "USD", True, D("2000.00")),
        ("cash", "EUR", True, D("-4.00")),
        ("system_fees", "EUR", False, D("4.00")),
    ])
    assert await _lines(db_session, trade.id) == sorted([
        ("position", "AAPL", True, D(10)),
        ("system_equity", "AAPL", False, D(-10)),
        ("cash", "USD", True, D("-2000.00")),
        ("system_equity", "USD", False, D("2000.00")),
        ("cash", "EUR", True, D("-2.60")),
        ("system_fees", "EUR", False, D("2.60")),
    ])
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("98393.40")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    assert res.fill.fee_amount == D("2.60") and res.fill.fee_currency == "EUR"


async def test_buy_usd_uses_existing_usd_cash_first(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, await _order(db_session, p, "AAPL", OrderSide.buy, qty=D(10)))
    qs.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    # sell 5 at 199.90 -> 999.50 USD stays in cash[USD]
    await _fill(db_session, qs, await _order(db_session, p, "AAPL", OrderSide.sell, qty=D(5)))
    res = await _fill(db_session, qs, await _order(db_session, p, "AAPL", OrderSide.buy, qty=D(10)))
    fx = res.journals[0]
    assert fx.type == JournalType.FX
    lines = await _lines(db_session, fx.id)
    # only the 1000.50 USD shortfall is converted: a = 1000.50 / 1.25 = 800.40
    assert ("cash", "USD", True, D("1000.50")) in lines
    assert ("cash", "EUR", True, D("-800.40")) in lines
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)


async def test_sell_reduces_long_and_realizes(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10)))
    qs.set_quote("ENI.MI", D("16.000"), D("16.010"), D("16.005"))
    res = await _fill(
        db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(4))
    )
    assert await _lines(db_session, res.journals[0].id) == sorted([
        ("position", "ENI.MI", True, D(-4)),
        ("system_equity", "ENI.MI", False, D(4)),
        ("cash", "EUR", True, D("64.00")),
        ("system_equity", "EUR", False, D("-64.00")),
        ("cash", "EUR", True, D("-1.06")),
        ("system_fees", "EUR", False, D("1.06")),
    ])
    # 99848.85 + 64 - 1.06
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("99911.79")
    pos = await db_session.scalar(select(Position).where(Position.portfolio_id == p.id))
    assert pos.qty == D(6) and pos.avg_price == D("15.000") and pos.realized_pnl == D(4)


async def test_sell_past_zero_not_allowed_yet(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10)))
    o = await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(11))
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, o)
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED


async def test_insufficient_cash_leverage_1(db_session, seeded, qs):
    p = await _portfolio(db_session)
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, notional=D(200000))
    with pytest.raises(SMTError) as e:
        async with db_session.begin_nested():
            await _fill(db_session, qs, o)
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    count = await db_session.scalar(
        select(func.count()).select_from(Journal).where(Journal.order_id == o.id)
    )
    assert count == 0
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(100000)


async def test_insufficient_cash_for_fx_leverage_1(db_session, seeded, qs):
    p = await _portfolio(db_session)
    # 600 AAPL = 120000 USD = 96000 EUR + fx fee 240 + trade fee 20 -> fits; 700 does not
    o = await _order(db_session, p, "AAPL", OrderSide.buy, qty=D(700))
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, o)
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER


async def test_leverage_2_borrows(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=D(2))
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, notional=D(150000))
    res = await _fill(db_session, qs, o)
    assert res.fill.qty == D(10000)
    fee = res.fill.fee_amount
    assert fee == D(20)  # 1 + 0.1% x 150000 = 151, capped by max_fee 20
    loan = await _bal(db_session, p.id, "EUR", AccountKind.margin_loan)
    assert loan == (D(150000) + fee - D(100000)).copy_negate() == D(-50020)
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)
    lines = await _lines(db_session, res.journals[0].id)
    assert ("margin_loan", "EUR", True, D("-50020.00")) in lines
    assert ("cash", "EUR", True, D("50020.00")) in lines


async def test_leverage_2_buying_power_exceeded(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=D(2))
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, notional=D(250000))
    with pytest.raises(SMTError) as e:
        await _fill(db_session, qs, o)
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER


async def test_sell_repays_loan(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=D(2))
    await _fill(
        db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10000))
    )
    qs.set_quote("ENI.MI", D("15.000"), D("15.010"), D("15.005"))
    res = await _fill(
        db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(4000))
    )
    # proceeds 60000, fee 20 -> net 59980; debt 50020 repaid in full
    lines = await _lines(db_session, res.journals[0].id)
    assert ("margin_loan", "EUR", True, D("50020.00")) in lines
    assert ("cash", "EUR", True, D("-50020.00")) in lines
    assert await _bal(db_session, p.id, "EUR", AccountKind.margin_loan) == D(0)
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("9960.00")


async def test_sell_repays_loan_partially(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=D(2))
    await _fill(
        db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10000))
    )
    qs.set_quote("ENI.MI", D("15.000"), D("15.010"), D("15.005"))
    await _fill(db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(1000)))
    # proceeds 15000 - fee 16 = 14984 repaid
    assert await _bal(db_session, p.id, "EUR", AccountKind.margin_loan) == D("-35036.00")
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D(0)


async def test_already_filled_returns_none(db_session, seeded, qs):
    p = await _portfolio(db_session)
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10))
    assert await _fill(db_session, qs, o) is not None
    assert await _fill(db_session, qs, o) is None
    fills = await db_session.scalar(
        select(func.count()).select_from(Fill).where(Fill.order_id == o.id)
    )
    assert fills == 1
    journals = await db_session.scalar(
        select(func.count()).select_from(Journal).where(Journal.order_id == o.id)
    )
    assert journals == 1
    assert await _bal(db_session, p.id, "EUR", AccountKind.cash) == D("99848.85")
    canceled = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(1),
                            status=OrderStatus.canceled)
    assert await _fill(db_session, qs, canceled) is None


async def test_limit_buy_never_worse_than_limit(db_session, seeded, qs):
    p = await _portfolio(db_session)
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10), type=OrderType.limit,
                     limit_price=D("14.500"), status=OrderStatus.pending)
    res = await _fill(db_session, qs, o)
    assert res.fill.price == D("14.500")


async def test_oco_sibling_canceled(db_session, seeded, qs):
    p = await _portfolio(db_session)
    await _fill(db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10)))
    group = uuid.uuid4()
    tp = await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(10), type=OrderType.limit,
                      limit_price=D("14.000"), status=OrderStatus.pending, oco_group_id=group)
    sl = await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(10), type=OrderType.stop,
                      stop_price=D("13.000"), status=OrderStatus.pending, oco_group_id=group)
    await _fill(db_session, qs, tp)
    await db_session.refresh(sl)
    assert sl.status == OrderStatus.canceled


async def test_bracket_children_activated(db_session, seeded, qs):
    p = await _portfolio(db_session)
    entry = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10), role="entry")
    group = uuid.uuid4()
    tp = await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(10), type=OrderType.limit,
                      limit_price=D("17.000"), status=OrderStatus.held, parent_id=entry.id,
                      oco_group_id=group, role="take_profit")
    sl = await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(10), type=OrderType.stop,
                      stop_price=D("13.000"), status=OrderStatus.held, parent_id=entry.id,
                      oco_group_id=group, role="stop_loss")
    await _fill(db_session, qs, entry)
    await db_session.refresh(tp)
    await db_session.refresh(sl)
    assert tp.status == sl.status == OrderStatus.pending
    assert tp.oco_group_id == sl.oco_group_id == group


async def _next_event(pubsub, wait_s=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while loop.time() < deadline:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg:
            return json.loads(msg["data"])
    return None


async def test_events_and_tracking_only_after_dispatch(db_session, seeded, qs, redis_url):
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.srem("md:tracked:positions", "ENI.MI")
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    try:
        p = await _portfolio(db_session)
        o = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(10))
        res = await _fill(db_session, qs, o)
        # nothing leaves the transaction before commit + dispatch
        assert await _next_event(pubsub, 0.3) is None
        assert "ENI.MI" not in await redis.smembers("md:tracked:positions")
        assert [e[0] for e in db_session.info[POST_COMMIT_KEY]] == ["event", "track"]

        # db_session is a rolled-back savepoint fixture: dispatch stands in for post-commit
        assert await dispatch_post_commit(db_session, redis) == 2
        assert POST_COMMIT_KEY not in db_session.info
        assert "ENI.MI" in await tracked_symbols(redis)
        assert "ENI.MI" in await redis.smembers("md:tracked:positions")
        event = await _next_event(pubsub)
        assert event is not None and event["type"] == "order.filled"
        assert event["order_id"] == str(o.id) and event["fill_id"] == str(res.fill.id)
        assert D(event["price"]) == D(15) and D(event["qty"]) == D(10)
        assert "ts" in event

        await _fill(
            db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.sell, qty=D(10))
        )
        assert "ENI.MI" in await redis.smembers("md:tracked:positions")
        assert await dispatch_post_commit(db_session, redis) == 2
        assert "ENI.MI" not in await redis.smembers("md:tracked:positions")
    finally:
        await pubsub.aclose()
        await redis.aclose()


async def test_discard_post_commit_drops_effects(db_session, seeded, qs, redis_url):
    redis = aioredis.from_url(redis_url, decode_responses=True)
    try:
        p = await _portfolio(db_session)
        await _fill(db_session, qs, await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=D(1)))
        discard_post_commit(db_session)
        assert await dispatch_post_commit(db_session, redis) == 0
    finally:
        await redis.aclose()


async def test_dispatch_post_commit_logs_and_continues(db_session, caplog):
    class Boom:
        async def publish(self, channel, message):
            raise RuntimeError("redis down")

    queue_event(db_session, "a", {})
    queue_event(db_session, "b", {})
    with caplog.at_level("ERROR", logger="smtrading.events"):
        assert await dispatch_post_commit(db_session, Boom()) == 0
    assert caplog.text.count("post-commit effect failed") == 2


async def test_publish_event_none_redis_is_noop():
    await publish_event(None, "x", {"a": D(1)})


async def test_notify_inserts_json_safe_notification(db_session, seeded):
    p = await _portfolio(db_session)
    oid = uuid.uuid4()
    ts = datetime(2026, 1, 2, tzinfo=UTC)
    n = await notify(db_session, portfolio_id=p.id, kind="margin.warning",
                     payload={"equity": D("1.50"), "order_id": oid, "at": ts}, agent_id="a1")
    row = await db_session.get(Notification, n.id)
    assert row.payload == {"equity": "1.50", "order_id": str(oid), "at": ts.isoformat()}
    assert row.agent_id == "a1" and row.kind == "margin.warning"


async def test_leverage_2_fx_then_borrows_in_instrument_currency(db_session, seeded, qs):
    p = await _portfolio(db_session, leverage=D(2))
    res = await _fill(
        db_session, qs, await _order(db_session, p, "AAPL", OrderSide.buy, qty=D(700))
    )
    fx, trade = res.journals
    bought = next(
        q for k, a, own, q in await _lines(db_session, fx.id) if (k, a, own) == ("cash", "USD", True)
    )
    # all EUR (minus the 20 EUR trade fee) is converted, the rest of 140000 USD is borrowed
    eur = await _bal(db_session, p.id, "EUR", AccountKind.cash)
    assert D(0) <= eur < D("0.05")
    assert await _bal(db_session, p.id, "USD", AccountKind.cash) == D(0)
    loan = await _bal(db_session, p.id, "USD", AccountKind.margin_loan)
    assert loan == (D(140000) - bought).copy_negate()
    assert ("margin_loan", "USD", True, loan) in await _lines(db_session, trade.id)


async def test_buying_power_check_uses_raw_value(db_session, seeded, qs):
    """Raw BP of -0.003 rounds to 0.00 for display but must still reject the fill."""
    p = await _portfolio(db_session, leverage=2)
    qs.set_quote("ISP.MI", D("3.000"), D("3.000"), D("3.000"))
    await _fill(db_session, qs, await _order(db_session, p, "ISP.MI", OrderSide.buy, qty=D(1)))
    c1 = await _bal(db_session, p.id, "EUR", AccountKind.cash)
    # after buying q ENI at ask A (mid M, fee F) with ISP marked at L:
    # bp = 2*(c1 - q*A - F + q*M + L) - (q*M + L) = 2*c1 - 2*q*A - 2*F + q*M + L
    q, a, m, f = D(13330), D("15.000"), D("14.995"), D(20)
    mark = D("-0.003") - (2 * c1 - 2 * q * a - 2 * f + q * m)
    assert mark > 0
    qs.set_quote("ISP.MI", None, None, mark)
    o = await _order(db_session, p, "ENI.MI", OrderSide.buy, qty=q)
    with pytest.raises(SMTError) as e:
        async with db_session.begin_nested():
            await _fill(db_session, qs, o)
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    assert D(e.value.details["buying_power_raw"]) == D("-0.003")
    assert await db_session.scalar(
        select(func.count()).select_from(Journal).where(Journal.order_id == o.id)
    ) == 0

    # +0.003 of headroom fills
    qs.set_quote("ISP.MI", None, None, mark + D("0.006"))
    discard_post_commit(db_session)
    assert await _fill(db_session, qs, o) is not None
