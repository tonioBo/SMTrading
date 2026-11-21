"""Plan 29: synthetic orders on a market-only broker (eToro demo, on the FakeLiveBroker).

Limit entries, stops, targets, OCO legs and trailing stops of a portfolio linked to a
broker that takes only market orders are accepted, kept and watched by SMTrading (the
processor, same quotes and guards as the simulator) and sent to the broker as a market
order when their trigger is reached; the broker's fill is applied as usual. What the
agents read is the order as they placed it: its own type and prices, the usual statuses.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.base import BrokerAdapter, BrokerOpenOrder, market_view
from smtrading.brokers.etoro_core import NOT_CLOSABLE_MESSAGE
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import (
    BrokerState,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    modify_order,
    place_bracket_order,
    place_oco_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.models import Notification
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal
TWO = D(2)
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
KEYS = {"api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-0002"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


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


@pytest.fixture
def qs():
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


@pytest.fixture
async def etoro():
    """(fake, http client on its transport): no request ever leaves the process."""
    from smtrading.brokers.live.fake import FakeLiveBroker

    fake = FakeLiveBroker(price=D(100))
    fake.etoro["demo"].credit = D(100000)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        yield fake, http


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _portfolio(maker) -> tuple[uuid.UUID, uuid.UUID]:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "etoro_demo", dict(KEYS))
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id, link.id


def _req(pid, **kw) -> PlaceOrderRequest:
    values = {"portfolio_id": pid, "symbol": "AAPL", "side": OrderSide.buy,
              "type": OrderType.market, "qty": TWO, **kw}
    return PlaceOrderRequest(**values)


async def _place(maker, redis, qs, pid, *, now=OPEN, **kw) -> uuid.UUID:
    async with maker() as s:
        order = await place_order(s, qs, _req(pid, **kw), now=now)
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


LIMIT, TP, SL = D("95.00"), D("110.00"), D("90.00")


async def _bracket(maker, redis, qs, pid, *, limit_price=LIMIT, tp=TP, sl=SL, **kw
                   ) -> list[uuid.UUID]:
    async with maker() as s:
        orders = await place_bracket_order(
            s, qs, _req(pid, type=OrderType.limit, limit_price=limit_price, **kw),
            take_profit=tp, stop_loss=sl, now=OPEN)
        await s.commit()
        await dispatch_post_commit(s, redis)
    return [o.id for o in orders]


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


async def _fills(maker, oid) -> list[Fill]:
    async with maker() as s:
        return list(await s.scalars(select(Fill).where(Fill.order_id == oid)))


async def _position(maker, pid) -> Decimal:
    async with maker() as s:
        return await s.scalar(select(Position.qty).where(
            Position.portfolio_id == pid, Position.symbol == "AAPL")) or D(0)


def _sync(maker, redis, qs, http, now=OPEN) -> BrokerSync:
    return BrokerSync(maker, redis, qs, http, clock=Clock(now))


async def _settle(sync: BrokerSync, n: int = 3) -> None:
    for _ in range(n):
        await sync.tick()


async def _tick(maker, redis, qs, bid, ask, *, now=OPEN, suspect=None) -> int:
    """One processor evaluation of AAPL at bid/ask (a new processor: no throttle)."""
    qs.set_quote("AAPL", D(bid), D(ask), None, ts=now, suspect=suspect)
    done = await OrderProcessor(maker, redis, qs, clock=Clock(now)).on_tick(
        "AAPL", qs._quotes["AAPL"])
    qs.set_quote("AAPL", D(bid), D(ask), None)  # fresh again for the next placements
    return done


def _sent(fake) -> list[str]:
    """The order requests (opens and closes) the broker received."""
    return [p for m, p in fake.calls if m == "POST" and (
        p.endswith("/orders") or "/market-close-orders/" in p)]


async def _hold(maker, redis, qs, pid, sync, fake) -> None:
    """A long position of 2 AAPL at the broker and here (a market buy)."""
    oid = await _place(maker, redis, qs, pid)
    await _settle(sync)
    assert (await _order(maker, oid)).status == OrderStatus.filled
    fake.calls.clear()


# -- the whole bracket --------------------------------------------------------------------

async def test_bracket_entry_triggers_then_stop_sells_at_market_and_cancels_target(
        maker, redis, qs, etoro):
    fake, http = etoro
    pid, link_id = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs, http)

    o = await _order(maker, entry)  # accepted, kept here: nothing at the broker
    assert (o.status, o.type, o.synthetic, o.broker_link_id) == (
        OrderStatus.pending, OrderType.limit, True, None)
    for child in (tp, sl):
        c = await _order(maker, child)
        assert (c.status, c.synthetic, c.broker_link_id) == (OrderStatus.held, True, None)
    await _settle(sync)
    assert _sent(fake) == []

    assert await _tick(maker, redis, qs, "95.90", "96.00") == 0  # above the limit
    assert (await _order(maker, entry)).broker_link_id is None

    fake.price = D("95")
    assert await _tick(maker, redis, qs, "94.90", "95.00") == 1  # ask <= limit
    o = await _order(maker, entry)
    assert (o.type, o.limit_price, o.status) == (OrderType.limit, D("95.00"),
                                                  OrderStatus.pending)
    assert o.broker_link_id == link_id and o.broker_status == BrokerState.TO_SEND
    await _settle(sync)
    o = await _order(maker, entry)
    assert (o.status, o.filled_qty, o.avg_fill_price) == (OrderStatus.filled, TWO, D(95))
    assert [(f.price, f.source) for f in await _fills(maker, entry)] == [(D(95), "broker")]
    opens = [p for p in _sent(fake) if p.endswith("/orders")]
    assert len(opens) == 1
    assert await _position(maker, pid) == TWO

    for child in (tp, sl):  # active, still kept here
        c = await _order(maker, child)
        assert (c.status, c.broker_link_id, c.synthetic) == (OrderStatus.pending, None, True)
    fake.calls.clear()
    await _settle(sync)
    assert _sent(fake) == []

    fake.price = D("89.90")
    assert await _tick(maker, redis, qs, "89.90", "90.00") == 1  # bid <= stop
    s = await _order(maker, sl)
    assert (s.type, s.stop_price, s.status) == (OrderType.stop, D("90.00"),
                                                 OrderStatus.triggered)
    assert s.broker_status == BrokerState.TO_SEND
    assert (await _order(maker, tp)).status == OrderStatus.canceled  # OCO, locally
    await _settle(sync)
    s = await _order(maker, sl)
    assert (s.status, s.type, s.filled_qty, s.avg_fill_price) == (
        OrderStatus.filled, OrderType.stop, TWO, D("89.90"))
    assert [p for p in _sent(fake) if "/market-close-orders/" in p]
    assert await _position(maker, pid) == 0
    assert fake.etoro["demo"].positions == {}


async def test_take_profit_sells_at_market_and_cancels_the_stop(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs, http)
    fake.price = D("95")
    await _tick(maker, redis, qs, "94.90", "95.00")
    await _settle(sync)
    assert (await _order(maker, entry)).status == OrderStatus.filled

    assert await _tick(maker, redis, qs, "109.90", "110.00") == 0  # bid below the target
    fake.price = D("110.20")
    assert await _tick(maker, redis, qs, "110.20", "110.30") == 1  # bid >= target
    assert (await _order(maker, sl)).status == OrderStatus.canceled
    await _settle(sync)
    t = await _order(maker, tp)
    assert (t.status, t.type, t.limit_price, t.avg_fill_price) == (
        OrderStatus.filled, OrderType.limit, D("110.00"), D("110.20"))
    assert await _position(maker, pid) == 0


async def test_market_bracket_entry_at_the_broker_keeps_its_exits_here(
        maker, redis, qs, etoro):
    """A market entry goes to eToro as before; its stop and target, which eToro would
    refuse, stay here once active."""
    _, http = etoro
    pid, _ = await _portfolio(maker)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, _req(pid), take_profit=D("110.00"), stop_loss=D("90.00"), now=OPEN)
        await s.commit()
    assert entry.broker_status == BrokerState.TO_SEND and not entry.synthetic
    await _settle(_sync(maker, redis, qs, http))
    assert (await _order(maker, entry.id)).status == OrderStatus.filled
    for child in (tp.id, sl.id):
        c = await _order(maker, child)
        assert (c.status, c.broker_link_id, c.synthetic) == (OrderStatus.pending, None, True)


# -- modify, OCO, standalone -----------------------------------------------------------------

async def test_modified_stop_triggers_at_its_new_level(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    stop = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                        stop_price=D("90.00"), tif=TimeInForce.gtc)
    async with maker() as s:
        o = await modify_order(s, qs, stop, stop_price=D("95.00"), now=OPEN)
        await s.commit()
    assert (o.stop_price, o.broker_link_id, o.broker_status) == (D("95.00"), None, None)
    fake.price = D("94.90")
    assert await _tick(maker, redis, qs, "94.90", "95.00") == 1
    await _settle(sync)
    o = await _order(maker, stop)
    assert (o.status, o.type, o.stop_price) == (OrderStatus.filled, OrderType.stop, D("95.00"))


async def test_oco_protection_one_leg_sent_the_other_canceled(maker, redis, qs, etoro):
    """PROTEGGI: an OCO of a stop and a target on a held position."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    async with maker() as s:
        stop, target = await place_oco_order(
            s, qs, _req(pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("90.00"),
                        tif=TimeInForce.gtc),
            _req(pid, side=OrderSide.sell, type=OrderType.limit, limit_price=D("120.00"),
                 tif=TimeInForce.gtc), now=OPEN)
        await s.commit()
    for o in (stop, target):
        assert (o.status, o.synthetic, o.broker_link_id) == (OrderStatus.pending, True, None)
    await _settle(sync)
    assert _sent(fake) == []
    fake.price = D("89")
    await _tick(maker, redis, qs, "89.00", "89.10")
    assert (await _order(maker, target.id)).status == OrderStatus.canceled
    await _settle(sync)
    assert (await _order(maker, stop.id)).status == OrderStatus.filled
    assert len(_sent(fake)) == 1


async def test_trailing_stop_keeps_its_type(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    oid = await _place(maker, redis, qs, pid, side=OrderSide.sell,
                       type=OrderType.trailing_stop, trail_pct=D(5), tif=TimeInForce.gtc)
    fake.price = D(90)
    await _tick(maker, redis, qs, "90.00", "90.10")
    await _settle(sync)
    o = await _order(maker, oid)
    assert (o.status, o.type, o.trail_pct) == (OrderStatus.filled, OrderType.trailing_stop,
                                                D(5))


async def test_short_sell_is_still_refused(maker, redis, qs, etoro):
    pid, _ = await _portfolio(maker)
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                     stop_price=D("90.00"))
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED
    assert e.value.message == NOT_CLOSABLE_MESSAGE
    with pytest.raises(SMTError) as e:  # a short bracket: its entry is a sell
        await _bracket(maker, redis, qs, pid, side=OrderSide.sell, limit_price=D("105.00"),
                       tp=D("95.00"), sl=D("115.00"))
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED


# -- what stops a trigger -------------------------------------------------------------------

async def test_valid_until_expires_the_entry_locally(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    until = OPEN + timedelta(hours=1)
    entry, tp, sl = await _bracket(maker, redis, qs, pid, valid_until=until.isoformat())
    await OrderProcessor(maker, redis, qs, clock=Clock(until)).sweep()
    assert (await _order(maker, entry)).status == OrderStatus.expired
    assert {(await _order(maker, c)).status for c in (tp, sl)} == {OrderStatus.canceled}
    await _settle(_sync(maker, redis, qs, http, now=until))
    assert _sent(fake) == []


async def test_kill_switch_refuses_entries_lets_exits_through(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    entry = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    stop = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                        stop_price=D("94.00"))
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    fake.price = D("93.90")
    await _tick(maker, redis, qs, "93.90", "94.00")
    e = await _order(maker, entry)
    assert (e.status, e.reject_code) == (OrderStatus.rejected, ErrorCode.KILL_SWITCH_ACTIVE)
    await _settle(sync)
    assert (await _order(maker, stop)).status == OrderStatus.filled
    assert len(_sent(fake)) == 1  # the exit only


async def test_suspect_quote_never_triggers(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    assert await _tick(maker, redis, qs, "80.00", "80.10", suspect="jump") == 0
    o = await _order(maker, entry)
    assert (o.status, o.broker_link_id) == (OrderStatus.pending, None)
    await _settle(_sync(maker, redis, qs, http))
    assert _sent(fake) == []


async def test_closed_market_never_triggers(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"),
                         tif=TimeInForce.gtc)
    assert await _tick(maker, redis, qs, "80.00", "80.10", now=SATURDAY) == 0
    qs.set_quote("AAPL", D("80.00"), D("80.10"), None, ts=SATURDAY)
    await OrderProcessor(maker, redis, qs, clock=Clock(SATURDAY)).sweep()
    assert (await _order(maker, entry)).broker_link_id is None
    await _settle(_sync(maker, redis, qs, http, now=SATURDAY))
    assert _sent(fake) == []


async def test_broker_refusal_rejects_notifies_and_cancels_the_exits(maker, redis, qs, etoro):
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry, tp, sl = await _bracket(maker, redis, qs, pid)
    fake.etoro["demo"].credit = D(1)  # «Insufficient funds»
    await _tick(maker, redis, qs, "94.90", "95.00")
    await _settle(_sync(maker, redis, qs, http))
    o = await _order(maker, entry)
    assert (o.status, o.reject_code) == (OrderStatus.rejected, ErrorCode.PROVIDER_UNAVAILABLE)
    assert {(await _order(maker, c)).status for c in (tp, sl)} == {OrderStatus.canceled}
    async with maker() as s:
        notes = list(await s.scalars(select(Notification).where(
            Notification.portfolio_id == pid, Notification.kind == "order.rejected")))
    assert len(notes) == 1


# -- reconciliation and protection -------------------------------------------------------

async def test_reconciliation_does_not_miss_resting_synthetic_orders(maker, redis, qs, etoro):
    from smtrading.reconciliation.compare import compare_orders
    from smtrading.reconciliation.service import _our_order, check_link

    fake, http = etoro
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                 stop_price=D("90.00"), tif=TimeInForce.gtc)
    rows = await check_link(maker, link_id, qs, http=http)
    items = rows[0].items
    assert [i for i in items if i["kind"] == "order"] == []
    assert next(i for i in items if i["key"] == "position:AAPL")["status"] == "ok"

    # once triggered it is at the broker as a market order: compared as one
    o = Order(id=uuid.uuid4(), portfolio_id=pid, symbol="AAPL", side=OrderSide.sell,
              type=OrderType.stop, qty=TWO, stop_price=D(90), filled_qty=D(0),
              status=OrderStatus.triggered, synthetic=True, broker_order_id="b1",
              broker_status="accepted", updated_at=OPEN - timedelta(hours=1))
    ours = _our_order(o)
    assert (ours.type, ours.stop_price, ours.limit_price) == ("market", None, None)
    items = compare_orders([ours], [BrokerOpenOrder("b1", "AAPL", "sell", TWO, None, None)],
                           OPEN)
    assert [i["status"] for i in items] == ["ok"]


async def test_synthetic_stop_counts_as_protection(maker, redis, qs, etoro):
    from smtrading.positions.protection import find_problems

    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    later = datetime.now(UTC) + timedelta(minutes=30)
    async with maker() as s:
        found, _ = await find_problems(s, later, ZoneInfo("Europe/Rome"))
    assert [f.position.portfolio_id for f in found] == [pid]  # unprotected
    await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                 stop_price=D("90.00"), tif=TimeInForce.gtc)
    async with maker() as s:
        found, _ = await find_problems(s, later, ZoneInfo("Europe/Rome"))
    assert found == []


# -- what the agents read --------------------------------------------------------------------

async def test_agent_view_has_the_order_as_placed(maker, redis, qs, etoro):
    from smtrading.mcp.dto import agent_view, order_dto, to_jsonable

    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry, _, _ = await _bracket(maker, redis, qs, pid)
    seen = agent_view(to_jsonable(order_dto(await _order(maker, entry))))
    assert "synthetic" not in seen
    assert (seen["type"], seen["status"], seen["limit_price"]) == ("limit", "pending", "95")
    fake.price = D("94.50")
    await _tick(maker, redis, qs, "94.40", "94.50")
    await _settle(_sync(maker, redis, qs, http))
    seen = agent_view(to_jsonable(order_dto(await _order(maker, entry))))
    assert (seen["type"], seen["status"], seen["avg_fill_price"]) == (
        "limit", "filled", "94.5")


def test_market_view_is_a_transient_market_copy():
    o = Order(id=uuid.uuid4(), portfolio_id=uuid.uuid4(), symbol="AAPL", side=OrderSide.buy,
              type=OrderType.stop_limit, qty=TWO, limit_price=D(1), stop_price=D(2),
              broker_attempt=1, synthetic=True)
    v = market_view(o)
    assert (v.id, v.qty, v.side, v.type, v.limit_price, v.stop_price) == (
        o.id, TWO, OrderSide.buy, OrderType.market, None, None)
    assert o.type == OrderType.stop_limit and o.limit_price == D(1)
    assert BrokerAdapter.client_ref(v) == f"{o.id}-1"
