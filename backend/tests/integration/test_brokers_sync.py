"""Broker routing (engine.service), BrokerSync and reconciliation with a fake adapter."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers import registry
from smtrading.brokers.base import BrokerAdapter, BrokerOrder
from smtrading.brokers.models import Reconciliation
from smtrading.brokers.service import create_link, link_portfolio, update_link
from smtrading.brokers.sync import BrokerSync, reconcile, reconcile_all
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import (
    BrokerState,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    cancel_order,
    modify_order,
    place_bracket_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import AssetClass
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import Notification
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed

D = Decimal
TEN = D(10)
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
SATURDAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
MONDAY_OPEN = datetime(2026, 11, 16, 14, 31, tzinfo=UTC)


class Broker:
    """State shared by every FakeBrokerAdapter instance (BrokerSync re-creates them)."""

    def __init__(self) -> None:
        self.orders: dict[str, BrokerOrder] = {}
        self.placed: list[tuple[str, str, Decimal | None]] = []  # (broker id, client ref, qty)
        self.canceled: list[str] = []
        self.positions: dict[str, Decimal] = {}
        self.reject_next: str | None = None
        self.fail_next = 0
        self.found: BrokerOrder | None = None
        self.cancel_fills: Decimal | None = None  # filled qty reported at cancel

    def set(self, broker_id: str, status: str, filled, avg) -> None:
        self.orders[broker_id] = BrokerOrder(
            broker_id, status, D(filled), None if avg is None else D(avg), None
        )


BROKER = Broker()


@registry.register
class FakeBrokerAdapter(BrokerAdapter):
    kind: ClassVar[str] = "fake_test"
    supported_types: ClassVar[frozenset[OrderType]] = frozenset(
        {OrderType.market, OrderType.limit, OrderType.stop, OrderType.stop_limit}
    )  # no trailing stop
    supported_asset_classes: ClassVar[frozenset[AssetClass]] = frozenset(AssetClass)

    async def place_order(self, order, instrument) -> BrokerOrder:
        if BROKER.fail_next:
            BROKER.fail_next -= 1
            raise RuntimeError("fake: HTTP 503: error")
        if BROKER.reject_next is not None:
            msg, BROKER.reject_next = BROKER.reject_next, None
            return BrokerOrder("", "rejected", D(0), None, msg)
        bid = f"b{len(BROKER.placed) + 1}"
        BROKER.placed.append((bid, self.client_ref(order), order.qty))
        BROKER.set(bid, "accepted", 0, None)
        return BROKER.orders[bid]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return BROKER.orders[broker_order_id]

    async def cancel_order(self, broker_order_id: str) -> None:
        BROKER.canceled.append(broker_order_id)
        o = BROKER.orders[broker_order_id]
        if o.status in ("accepted", "partially_filled"):
            filled = BROKER.cancel_fills if BROKER.cancel_fills is not None else o.filled_qty
            avg = o.avg_fill_price
            BROKER.orders[broker_order_id] = BrokerOrder(
                broker_order_id, "canceled", filled, avg, None
            )

    async def get_positions(self) -> dict[str, Decimal]:
        return dict(BROKER.positions)

    async def find_order(self, order, instrument, since):
        return BROKER.found


@registry.register
class FakeT212Adapter(FakeBrokerAdapter):
    kind: ClassVar[str] = "fake_t212_test"
    idempotent_place: ClassVar[bool] = False


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    global BROKER
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    BROKER = Broker()
    yield


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


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _linked_portfolio(maker, kind="fake_test", cash=100000):
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"P-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), leverage=D(1),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", kind, {})
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id, link.id


async def _place(maker, redis, qs, pid, *, now=OPEN, type=OrderType.market, qty=TEN,
                 side=OrderSide.buy, **kw):
    async with maker() as s:
        order = await place_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=side, type=type, qty=qty, **kw
            ), now=now,
        )
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


async def _trade_journals(maker, oid) -> list[Journal]:
    async with maker() as s:
        return list(await s.scalars(
            select(Journal).where(Journal.order_id == oid, Journal.type == JournalType.TRADE)
        ))


async def _fills(maker, oid) -> list[Fill]:
    async with maker() as s:
        return list(await s.scalars(select(Fill).where(Fill.order_id == oid)))


async def _notes(maker, pid, kind) -> list[Notification]:
    async with maker() as s:
        return list(await s.scalars(
            select(Notification).where(
                Notification.portfolio_id == pid, Notification.kind == kind
            )
        ))


def _sync(maker, redis, qs, now=OPEN):
    return BrokerSync(maker, redis, qs, None, clock=Clock(now))


async def test_routed_order_partial_then_full_fill(maker, redis, qs):
    """Review Focus 2: 5/10 -> one journal, partially_filled; 10/10 -> second journal,
    filled, weighted average price."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and o.broker_status == BrokerState.TO_SEND
    assert not o.broker_replicated and await _trade_journals(maker, oid) == []

    sync = _sync(maker, redis, qs)
    await sync.tick()
    o = await _order(maker, oid)
    assert o.broker_replicated and o.broker_order_id == "b1"
    assert o.broker_status == "accepted" and await _trade_journals(maker, oid) == []
    assert BROKER.placed == [("b1", str(oid), TEN)]

    BROKER.set("b1", "partially_filled", 5, "100")
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.partially_filled and o.filled_qty == 5
    assert len(await _trade_journals(maker, oid)) == 1

    await sync.tick()  # nothing new: no second journal
    assert len(await _trade_journals(maker, oid)) == 1

    BROKER.set("b1", "filled", 10, "101")  # the second 5 at 102
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and o.filled_qty == 10
    assert o.avg_fill_price == D("101")
    assert len(await _trade_journals(maker, oid)) == 2
    fills = sorted(await _fills(maker, oid), key=lambda f: f.price)
    assert [(f.qty, f.price, f.source) for f in fills] == [
        (5, D(100), "broker"), (5, D(102), "broker")
    ]
    assert BROKER.placed == [("b1", str(oid), TEN)]  # sent once


async def test_partial_without_broker_price_waits(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "partially_filled", 4, None)  # e.g. a Trading 212 pending order
    await sync.tick()
    o = await _order(maker, oid)
    assert o.filled_qty == 0 and await _trade_journals(maker, oid) == []
    BROKER.set("b1", "filled", 10, "99")
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and o.avg_fill_price == D(99)
    assert len(await _trade_journals(maker, oid)) == 1


async def test_broker_rejection_rejects_and_notifies(maker, redis, qs):
    """Review Focus 1."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    BROKER.reject_next = "insufficient buying power at broker"
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected
    assert o.reject_code == ErrorCode.PROVIDER_UNAVAILABLE
    assert o.reject_message == "insufficient buying power at broker"
    notes = await _notes(maker, pid, "order.rejected")
    assert len(notes) == 1 and notes[0].payload["message"] == o.reject_message
    assert await _trade_journals(maker, oid) == []


async def test_local_checks_run_before_routing(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, cash=100)
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid)  # 10 x 100 USD > 100 EUR
    assert e.value.code == ErrorCode.INSUFFICIENT_BUYING_POWER
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    pid2, _ = await _linked_portfolio(maker)
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid2)
    assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert BROKER.placed == []


async def test_kill_switch_and_lock_checked_again_before_sending(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        (await s.get(Portfolio, pid)).status = PortfolioStatus.locked
        await s.commit()
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.PORTFOLIO_LOCKED
    assert BROKER.placed == []


async def test_trailing_stop_on_broker_without_trailing_is_simulated(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    # a long position first (routed market buy, filled at the broker)
    buy = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    await sync.tick()
    assert (await _order(maker, buy)).status == OrderStatus.filled

    oid = await _place(
        maker, redis, qs, pid, type=OrderType.trailing_stop, side=OrderSide.sell,
        trail_pct=D(5),
    )
    o = await _order(maker, oid)
    assert o.broker_link_id is None and not o.broker_replicated
    assert o.status == OrderStatus.pending and o.trail_ref_price is not None
    await sync.tick()
    assert len(BROKER.placed) == 1  # not sent

    # the simulator executes it
    qs.set_quote("AAPL", D("90"), D("90.10"), None, ts=OPEN)
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    assert await proc.on_tick("AAPL", qs._quotes["AAPL"]) == 1
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled
    assert [f.source for f in await _fills(maker, oid)] == ["sim"]


async def test_processor_skips_broker_managed_orders(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    qs.set_quote("AAPL", D("90"), D("90.10"), None, ts=OPEN)
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    await proc.sweep()
    assert await proc.on_tick("AAPL", qs._quotes["AAPL"]) == 0
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and await _fills(maker, oid) == []


async def test_cancel_is_propagated(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        o = await cancel_order(s, oid, by="agent")
        await s.commit()
    assert o.status == OrderStatus.pending and o.broker_status == BrokerState.CANCEL_REQUESTED
    await sync.tick()
    assert BROKER.canceled == ["b1"]
    o = await _order(maker, oid)
    assert o.status == OrderStatus.canceled and o.broker_status == "canceled"


async def test_cancel_applies_last_fills(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "partially_filled", 3, "95")
    async with maker() as s:
        await cancel_order(s, oid, by="agent")
        await s.commit()
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.canceled and o.filled_qty == 3
    assert len(await _trade_journals(maker, oid)) == 1


async def test_cancel_before_sending_is_local(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    async with maker() as s:
        o = await cancel_order(s, oid, by="agent")
        await s.commit()
    assert o.status == OrderStatus.canceled
    await _sync(maker, redis, qs).tick()
    assert BROKER.placed == [] and BROKER.canceled == []


async def test_modify_cancels_and_resends(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        o = await modify_order(s, qs, oid, limit_price=D("96"), now=OPEN)
        await s.commit()
    assert o.broker_status == BrokerState.REPLACE_REQUESTED and o.limit_price == D(96)
    async with maker() as s:  # a second change waits for the broker
        with pytest.raises(SMTError) as e:
            await modify_order(s, qs, oid, limit_price=D("97"), now=OPEN)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE
    await sync.tick()  # cancel b1 -> to_send
    await sync.tick()  # send again with a new client reference
    assert BROKER.canceled == ["b1"]
    assert [p[1] for p in BROKER.placed] == [str(oid), f"{oid}-1"]
    o = await _order(maker, oid)
    assert o.broker_order_id == "b2" and o.status == OrderStatus.pending


async def test_queued_at_close_is_sent_at_the_open(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    o = await _order(maker, oid)
    assert o.status == OrderStatus.queued and o.broker_status == BrokerState.TO_SEND
    await _sync(maker, redis, qs, now=SATURDAY).tick()
    assert BROKER.placed == []
    await _sync(maker, redis, qs, now=MONDAY_OPEN).tick()
    assert len(BROKER.placed) == 1
    assert (await _order(maker, oid)).broker_order_id == "b1"


async def test_idempotent_send_failure_is_retried(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    BROKER.fail_next = 1
    sync = _sync(maker, redis, qs)
    await sync.tick()
    assert (await _order(maker, oid)).broker_status == BrokerState.TO_SEND
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id == "b1"


async def test_non_idempotent_failure_is_never_resent(maker, redis, qs):
    """Ruling R7: unknown outcome -> no resend; matched by find_order, or rejected
    after the match window."""
    pid, _ = await _linked_portfolio(maker, kind="fake_t212_test")
    oid = await _place(maker, redis, qs, pid)
    BROKER.fail_next = 1
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    await sync.tick()
    o = await _order(maker, oid)
    assert o.broker_status == BrokerState.UNKNOWN and o.broker_order_id is None
    assert len(await _notes(maker, pid, "broker.order_unknown")) == 1
    await sync.tick()
    assert BROKER.placed == []  # never resent

    BROKER.set("t9", "filled", 10, "100")
    BROKER.found = BROKER.orders["t9"]
    await sync.tick()
    o = await _order(maker, oid)
    assert o.broker_order_id == "t9" and o.status == OrderStatus.filled

    oid2 = await _place(maker, redis, qs, pid)
    BROKER.fail_next, BROKER.found = 1, None
    await sync.tick()
    await sync.tick()
    assert (await _order(maker, oid2)).status == OrderStatus.pending
    clock.now = OPEN + timedelta(minutes=6)
    await sync.tick()
    o2 = await _order(maker, oid2)
    assert o2.status == OrderStatus.rejected and "unknown" in o2.reject_message
    assert BROKER.placed == []


async def test_bracket_exits_follow_the_entry_to_the_broker(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    async with maker() as s:
        _entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=TEN,
            ), take_profit=D("110"), stop_loss=D("90"), now=OPEN,
        )
        await s.commit()
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    await sync.tick()  # entry filled: exits pending, to send
    await sync.tick()  # exits sent
    tp, sl = await _order(maker, tp.id), await _order(maker, sl.id)
    assert {tp.broker_order_id, sl.broker_order_id} == {"b2", "b3"}
    BROKER.set(tp.broker_order_id, "filled", 10, "110")
    await sync.tick()  # take profit filled: stop loss cancel requested
    assert (await _order(maker, sl.id)).broker_status == BrokerState.CANCEL_REQUESTED
    await sync.tick()
    assert sl.broker_order_id in BROKER.canceled
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled


async def test_disabled_link_releases_unsent_orders_to_the_simulator(maker, redis, qs):
    pid, link_id = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        await update_link(s, link_id, enabled=False)
        await s.commit()
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.broker_link_id is None and o.status == OrderStatus.queued
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    qs.set_quote("AAPL", D("99.90"), D("100.00"), None, ts=OPEN)
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_reconciliation_detects_a_difference(maker, redis, qs):
    pid, link_id = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    await sync.tick()
    assert (await _order(maker, oid)).status == OrderStatus.filled

    BROKER.positions = {"AAPL": D(10)}
    async with maker() as s:
        rows = await reconcile(s, link_id, None)
        await s.commit()
    assert [r.ok for r in rows] == [True]

    BROKER.positions = {"AAPL": D(7), "MSFT": D(1)}
    assert await reconcile_all(maker, None) == 1
    async with maker() as s:
        last = (await s.scalars(
            select(Reconciliation).where(Reconciliation.ok.is_(False))
        )).one()
    assert last.portfolio_id == pid
    assert last.diffs == [
        {"symbol": "AAPL", "ours": "10", "broker": "7"},
        {"symbol": "MSFT", "ours": "0", "broker": "1"},
    ]
