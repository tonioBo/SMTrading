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
    t = await _order(maker, tp)  # OCO: suspended while the stop is in flight
    assert (t.status, t.broker_link_id) == (OrderStatus.pending, None)
    await _settle(sync)
    assert (await _order(maker, tp)).status == OrderStatus.canceled  # once it filled
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
    assert (await _order(maker, sl)).status == OrderStatus.pending  # suspended
    await _settle(sync)
    assert (await _order(maker, sl)).status == OrderStatus.canceled
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
    t = await _order(maker, target.id)  # suspended, still protecting, not triggerable
    assert (t.status, t.broker_link_id) == (OrderStatus.pending, None)
    assert await _tick(maker, redis, qs, "121.00", "121.10") == 0
    await _settle(sync)
    assert (await _order(maker, stop.id)).status == OrderStatus.filled
    assert (await _order(maker, target.id)).status == OrderStatus.canceled
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


# -- hardening (plan 33) ---------------------------------------------------------------------

async def _oco_protection(maker, qs, pid, *, qty=TWO) -> tuple[uuid.UUID, uuid.UUID]:
    stop, target = D("90.00"), D("120.00")
    async with maker() as s:
        a, b = await place_oco_order(
            s, qs, _req(pid, side=OrderSide.sell, type=OrderType.stop, stop_price=stop,
                        qty=qty, tif=TimeInForce.gtc),
            _req(pid, side=OrderSide.sell, type=OrderType.limit, limit_price=target,
                 qty=qty, tif=TimeInForce.gtc), now=OPEN)
        await s.commit()
    return a.id, b.id


def _broker_units(fake) -> Decimal:
    return sum((p["units"] for p in fake.etoro["demo"].positions.values()), D(0))


async def test_oco_sibling_is_active_again_when_the_leg_is_refused(maker, redis, qs, etoro):
    """A: the stop goes to eToro and is refused there: the target was only suspended, it
    protects the position again (and the owner is told of the refusal)."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    stop, target = await _oco_protection(maker, qs, pid)
    fake.etoro["demo"].positions.clear()  # nothing closable at the broker: refused
    fake.price = D("89")
    assert await _tick(maker, redis, qs, "89.00", "89.10") == 1
    await _settle(sync)
    s = await _order(maker, stop)
    assert (s.status, s.reject_message) == (OrderStatus.rejected, NOT_CLOSABLE_MESSAGE)
    async with maker() as ses:
        kinds = list(await ses.scalars(select(Notification.kind).where(
            Notification.portfolio_id == pid)))
    assert "order.rejected" in kinds
    t = await _order(maker, target)
    assert (t.status, t.broker_link_id, t.qty) == (OrderStatus.pending, None, TWO)
    assert await _tick(maker, redis, qs, "120.10", "120.20") == 1  # active again
    assert (await _order(maker, target)).broker_status == BrokerState.TO_SEND


async def test_oco_sibling_follows_a_partial_fill(maker, redis, qs, etoro):
    """A: the stop closes only one of the two eToro positions: the target stays, sized
    to what is left."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    for _ in range(2):  # two positions of 1 at the broker
        await _place(maker, redis, qs, pid, qty=D(1))
        await _settle(sync)
    assert await _position(maker, pid) == TWO
    fake.calls.clear()
    stop, target = await _oco_protection(maker, qs, pid)
    fake.etoro_fail_close_after = 1  # the second close fails: a partial
    fake.price = D("89")
    await _tick(maker, redis, qs, "89.00", "89.10")
    await _settle(sync)
    s = await _order(maker, stop)
    assert s.filled_qty == D(1) and s.status in (OrderStatus.canceled,
                                                 OrderStatus.partially_filled)
    assert await _position(maker, pid) == D(1)
    t = await _order(maker, target)
    assert (t.status, t.qty, t.broker_link_id) == (OrderStatus.pending, D(1), None)


async def test_oco_entries_triggered_at_placement_send_one_leg_only(maker, redis, qs, etoro):
    """A: both legs marketable at placement: the first goes, the second waits."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    async with maker() as s:
        a, b = await place_oco_order(
            s, qs, _req(pid, type=OrderType.limit, limit_price=D("101.00")),
            _req(pid, type=OrderType.stop, stop_price=D("99.50")), now=OPEN)
        await s.commit()
    assert (a.broker_status, b.broker_status, b.status) == (
        BrokerState.TO_SEND, None, OrderStatus.pending)
    await _settle(_sync(maker, redis, qs, http))
    assert (await _order(maker, a.id)).status == OrderStatus.filled
    assert (await _order(maker, b.id)).status == OrderStatus.canceled
    assert len(_sent(fake)) == 1


async def _shared(maker, n=2) -> tuple[list[uuid.UUID], uuid.UUID]:
    async with maker() as s:
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "etoro_demo", dict(KEYS))
        pids = []
        for _ in range(n):
            p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                       initial_cash=D(100000), leverage=D(1))
            await link_portfolio(s, p.id, link.id)
            pids.append(p.id)
        await s.commit()
    return pids, link.id


async def test_shared_link_stop_is_clamped_to_its_portfolio_position(
        maker, redis, qs, etoro):
    """B: on a link shared by two portfolios a stop larger than what its portfolio still
    holds sells only that (eToro closes FIFO on the whole account); with nothing left it
    is canceled without sending anything."""
    fake, http = etoro
    (p1, p2), _ = await _shared(maker)
    sync = _sync(maker, redis, qs, http)
    for pid in (p1, p2):
        await _hold(maker, redis, qs, pid, sync, fake)
    assert _broker_units(fake) == D(4)
    stop = await _place(maker, redis, qs, p1, side=OrderSide.sell, type=OrderType.stop,
                        stop_price=D("90.00"), tif=TimeInForce.gtc)
    await _place(maker, redis, qs, p1, side=OrderSide.sell, qty=D(1))
    await _settle(sync)
    assert await _position(maker, p1) == D(1)
    fake.price = D("89")
    await _tick(maker, redis, qs, "89.00", "89.10")
    assert (await _order(maker, stop)).qty == D(1)
    await _settle(sync)
    assert (await _order(maker, stop)).status == OrderStatus.filled
    assert await _position(maker, p1) == 0
    assert _broker_units(fake) == TWO  # p2's position untouched

    # nothing left to reduce at the trigger: canceled, nothing sent
    other = await _place(maker, redis, qs, p2, side=OrderSide.sell, type=OrderType.stop,
                         stop_price=D("80.00"), tif=TimeInForce.gtc)
    await _place(maker, redis, qs, p2, side=OrderSide.sell)  # p2 sells it all meanwhile
    await _settle(sync)
    assert await _position(maker, p2) == 0
    fake.calls.clear()
    fake.price = D("79")
    await _tick(maker, redis, qs, "79.00", "79.10")
    await _settle(sync)
    o = await _order(maker, other)
    assert (o.status, o.reject_message) == (OrderStatus.canceled, "nothing_to_reduce")
    assert _sent(fake) == []


async def test_shared_link_two_sells_in_flight_never_oversell(maker, redis, qs, etoro):
    """B: a market sell and a triggered stop of the same portfolio both waiting to be
    sent: once the first is at the broker the second waits, then has nothing to reduce."""
    fake, http = etoro
    (p1, p2), _ = await _shared(maker)
    sync = _sync(maker, redis, qs, http)
    for pid in (p1, p2):
        await _hold(maker, redis, qs, pid, sync, fake)
    stop = await _place(maker, redis, qs, p1, side=OrderSide.sell, type=OrderType.stop,
                        stop_price=D("90.00"), tif=TimeInForce.gtc)
    sell = await _place(maker, redis, qs, p1, side=OrderSide.sell)  # to send
    fake.price = D("89")
    await _tick(maker, redis, qs, "89.00", "89.10")
    assert (await _order(maker, stop)).broker_status == BrokerState.TO_SEND
    await _settle(sync, 4)
    statuses = {(await _order(maker, o)).status for o in (stop, sell)}
    assert statuses == {OrderStatus.filled, OrderStatus.canceled}
    assert await _position(maker, p1) == 0
    assert await _position(maker, p2) == TWO
    assert _broker_units(fake) == TWO


async def test_triggered_limit_no_longer_marketable_goes_back_to_watching(
        maker, redis, qs, etoro):
    """C: the price moved away before the send: never sent at market, watched again."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    entry = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"),
                         tif=TimeInForce.gtc)
    await _tick(maker, redis, qs, "94.90", "95.00")
    assert (await _order(maker, entry)).broker_status == BrokerState.TO_SEND
    qs.set_quote("AAPL", D("96.90"), D("97.00"), None)  # ask above the limit now
    sync = _sync(maker, redis, qs, http)
    await _settle(sync)
    o = await _order(maker, entry)
    assert (o.status, o.broker_link_id, o.broker_status, o.type) == (
        OrderStatus.pending, None, None, OrderType.limit)
    assert _sent(fake) == []
    fake.price = D("95")
    assert await _tick(maker, redis, qs, "94.90", "95.00") == 1
    await _settle(sync)
    assert (await _order(maker, entry)).status == OrderStatus.filled

    # a stop-limit whose stop was hit goes back as triggered (a resting limit)
    sl = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop_limit,
                      stop_price=D("90.00"), limit_price=D("89.00"), tif=TimeInForce.gtc)
    await _tick(maker, redis, qs, "89.90", "90.00")  # stop hit
    await _tick(maker, redis, qs, "89.90", "90.00")  # bid >= limit: handed
    assert (await _order(maker, sl)).broker_status == BrokerState.TO_SEND
    qs.set_quote("AAPL", D("88.00"), D("88.10"), None)
    fake.calls.clear()
    await _settle(sync)
    o = await _order(maker, sl)
    assert (o.status, o.broker_link_id, o.broker_status) == (
        OrderStatus.triggered, None, None)
    assert _sent(fake) == []


async def test_plan_closer_sees_a_handed_limit_as_in_flight(maker, redis, qs, etoro):
    """D: a target triggered and waiting to be sent counts as a close in flight."""
    from types import SimpleNamespace

    from smtrading.goals.plan_closer import PlanCloser

    fake, http = etoro
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _hold(maker, redis, qs, pid, sync, fake)
    target = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.limit,
                          limit_price=D("120.00"), tif=TimeInForce.gtc)
    row = SimpleNamespace(portfolio_id=pid, symbol="AAPL")
    closer = PlanCloser(maker, qs, redis, tz="Europe/Rome")
    async with maker() as s:
        assert not await closer._close_in_flight(s, row, TWO)
    await _tick(maker, redis, qs, "120.10", "120.20")
    o = await _order(maker, target)
    assert (o.status, o.broker_status) == (OrderStatus.pending, BrokerState.TO_SEND)
    async with maker() as s:
        assert await closer._close_in_flight(s, row, TWO)


async def test_a_handed_order_cannot_be_modified(maker, redis, qs, etoro):
    """E: once handed for sending, a change would be ignored: refused."""
    pid, _ = await _portfolio(maker)
    entry = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"))
    await _tick(maker, redis, qs, "94.90", "95.00")
    with pytest.raises(SMTError) as e:
        async with maker() as s:
            await modify_order(s, qs, entry, limit_price=D("94.00"), now=OPEN)
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert "synthetic" not in e.value.message
    assert (await _order(maker, entry)).limit_price == D("95.00")


async def test_oco_with_a_leg_beyond_the_position_is_refused_whole(maker, redis, qs, etoro):
    """F: one leg would sell more than held: both legs are refused, none left alone."""
    fake, http = etoro
    pid, _ = await _portfolio(maker)
    await _hold(maker, redis, qs, pid, _sync(maker, redis, qs, http), fake)
    with pytest.raises(SMTError) as e:
        async with maker() as s:
            try:
                await place_oco_order(
                    s, qs, _req(pid, side=OrderSide.sell, type=OrderType.stop,
                                stop_price=D("90.00")),
                    _req(pid, side=OrderSide.sell, type=OrderType.limit,
                         limit_price=D("120.00"), qty=D(3)), now=OPEN)
            finally:
                await s.commit()
    assert e.value.code == ErrorCode.SHORT_NOT_ALLOWED
    async with maker() as s:
        legs = list(await s.scalars(select(Order).where(
            Order.portfolio_id == pid, Order.side == OrderSide.sell)))
    assert len(legs) == 2 and {o.status for o in legs} == {OrderStatus.rejected}


async def test_relink_to_a_broker_with_resting_orders_needs_them_canceled(
        maker, redis, qs, etoro):
    """G: kept orders only make sense on a market-only link: relinking to another eToro
    link is fine, to a broker that takes resting orders is refused."""
    pid, _ = await _portfolio(maker)
    await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95.00"),
                 tif=TimeInForce.gtc)
    async with maker() as s:
        other = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "etoro_demo", dict(KEYS))
        alpaca = await create_link(s, f"A-{uuid.uuid4().hex[:8]}", "alpaca_paper",
                                   {"key_id": "PK", "secret_key": "SK"})
        await link_portfolio(s, pid, other.id)
        await s.commit()
    with pytest.raises(SMTError) as e:
        async with maker() as s:
            await link_portfolio(s, pid, alpaca.id)
    assert e.value.code == ErrorCode.INVALID_ORDER
    async with maker() as s:  # unlinked (simulator), then to Alpaca: refused too
        await link_portfolio(s, pid, None)
        await s.commit()
    with pytest.raises(SMTError):
        async with maker() as s:
            await link_portfolio(s, pid, alpaca.id)


async def test_live_cash_estimate_of_a_triggered_stop_buy_uses_the_ask(maker, monkeypatch):
    """H: the cash check of a triggered stop buy values it at market, not at its stop."""
    from types import SimpleNamespace

    import smtrading.brokers.sync as sync_mod
    from smtrading.instruments.models import Instrument
    from smtrading.portfolios.models import Portfolio

    seen = []

    async def nothing(*a, **k):
        return None

    async def estimate(session, qs, *, portfolio, instrument, order):
        seen.append((order.type, order.stop_price))
        return D(1), D(0)

    for name in ("check_live_state", "check_live_cash", "check_live_fuse"):
        monkeypatch.setattr(sync_mod, name, nothing)
    monkeypatch.setattr(sync_mod, "snapshot_fresh", lambda link, now: True)
    monkeypatch.setattr(sync_mod, "estimate_buy", estimate)
    pid = (await _portfolio(maker))[0]
    async with maker() as s:
        portfolio = await s.get(Portfolio, pid)
        instrument = await s.get(Instrument, "AAPL")
        order = Order(id=uuid.uuid4(), portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                      type=OrderType.stop, qty=TWO, stop_price=D(90), filled_qty=D(0),
                      status=OrderStatus.triggered, synthetic=True, reduce_only=False,
                      live_flatten=False)
        sync = BrokerSync(maker, None, FakeQuoteSource(), None, clock=Clock(OPEN))
        assert await sync._live_refusal(s, None, portfolio, order, SimpleNamespace(),
                                        instrument, OPEN) is None
    assert seen == [(OrderType.market, None)]
