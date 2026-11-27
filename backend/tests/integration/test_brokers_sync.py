"""Broker routing (engine.service), BrokerSync and reconciliation with a fake adapter."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.admin.portfolio_ops import reset_portfolio
from smtrading.brokers import registry
from smtrading.brokers import sync as sync_module
from smtrading.brokers.base import BrokerAdapter, BrokerOrder, BrokerUnreachable
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.service import create_link, link_portfolio, update_link
from smtrading.brokers.sync import BrokerSync, reconcile, reconcile_all
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine import execution as execution_module
from smtrading.engine.execution import execute_fill
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
    close_position,
    modify_order,
    place_bracket_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import AssetClass
from smtrading.ledger.models import (
    Account,
    AccountBalance,
    AccountKind,
    Journal,
    JournalType,
)
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk import halts
from smtrading.risk.margin import check_portfolio_margin
from smtrading.risk.models import Notification, TradingHalt
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

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
        self.cancel_fills: Decimal | None = None  # filled qty reported at cancel
        self.by_ref: dict[str, str] = {}  # client reference -> broker id
        self.fail_after_send = 0  # placed, then the response is lost
        self.cancel_raises = False
        self.unsupported = False  # supports() false after prepare()
        self.candidates: list[str] = []  # non-idempotent find_order matches
        self.ignore_exclude = False
        self.finds = 0
        self.down_kinds: set[str] = set()  # adapters whose broker does not answer
        self.attempts = 0  # place_order calls (answered or not)
        self.find_raises: Exception | None = None
        self.read_timeouts: set[str] = set()  # client refs whose lookup times out reading
        self.reconcile_raises: Exception | None = None

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

    def routable(self, order, instrument) -> bool:
        return BrokerAdapter.supports(self, order, instrument) and order.notional is None

    def supports(self, order, instrument) -> bool:
        return self.routable(order, instrument) and not BROKER.unsupported

    def _down(self) -> None:
        if self.kind in BROKER.down_kinds:
            raise BrokerUnreachable(f"{self.kind}: ConnectTimeout", connect=True)

    async def place_order(self, order, instrument) -> BrokerOrder:
        BROKER.attempts += 1
        self._down()
        if BROKER.fail_next:
            BROKER.fail_next -= 1
            raise RuntimeError("fake: HTTP 503: error")
        if BROKER.reject_next is not None:
            msg, BROKER.reject_next = BROKER.reject_next, None
            return BrokerOrder("", "rejected", D(0), None, msg)
        bid = f"b{len(BROKER.placed) + 1}"
        BROKER.placed.append((bid, self.client_ref(order), order.qty))
        BROKER.by_ref[self.client_ref(order)] = bid
        BROKER.set(bid, "accepted", 0, None)
        if BROKER.fail_after_send:
            BROKER.fail_after_send -= 1
            raise RuntimeError("fake: ReadTimeout")
        return BROKER.orders[bid]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        self._down()
        return BROKER.orders[broker_order_id]

    async def cancel_order(self, broker_order_id: str) -> None:
        if BROKER.cancel_raises:
            raise RuntimeError("fake: HTTP 400: not cancelable")
        BROKER.canceled.append(broker_order_id)
        o = BROKER.orders[broker_order_id]
        if o.status in ("accepted", "partially_filled"):
            filled = BROKER.cancel_fills if BROKER.cancel_fills is not None else o.filled_qty
            avg = o.avg_fill_price
            BROKER.orders[broker_order_id] = BrokerOrder(
                broker_order_id, "canceled", filled, avg, None
            )

    async def get_positions(self) -> dict[str, Decimal]:
        self._down()
        if BROKER.reconcile_raises is not None:
            raise BROKER.reconcile_raises
        return dict(BROKER.positions)

    async def find_order(self, order, instrument, since, exclude=frozenset()):
        BROKER.finds += 1  # by client reference, like Alpaca
        self._down()
        if BROKER.find_raises is not None:
            raise BROKER.find_raises
        if self.client_ref(order) in BROKER.read_timeouts:
            raise BrokerUnreachable(f"{self.kind}: ReadTimeout")
        bid = BROKER.by_ref.get(self.client_ref(order))
        return None if bid is None or bid in exclude else BROKER.orders[bid]


@registry.register
class FakeOtherAdapter(FakeBrokerAdapter):
    kind: ClassVar[str] = "fake_other_test"


@registry.register
class FakeT212Adapter(FakeBrokerAdapter):
    kind: ClassVar[str] = "fake_t212_test"
    idempotent_place: ClassVar[bool] = False

    async def find_order(self, order, instrument, since, exclude=frozenset()):
        BROKER.finds += 1  # by ticker/side/qty/time: any equal order matches
        hits = [b for b in BROKER.candidates if BROKER.ignore_exclude or b not in exclude]
        return BROKER.orders[hits[0]] if len(hits) == 1 else None


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


async def test_broker_rejection_with_nul_in_message(maker, redis, qs):
    """A NUL in the broker's message never makes the order UPDATE fail: it is dropped."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    BROKER.reject_next = "rejected\x00 by broker"
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert (o.status, o.reject_message) == (OrderStatus.rejected, "rejected by broker")
    assert len(await _notes(maker, pid, "order.rejected")) == 1


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


async def test_trading_halt_checked_again_before_sending(maker, redis, qs):
    """M6: an opening that is still waiting to be sent when a halt starts (written
    directly: the halt's cancel pass did not see it) is refused before the send."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        s.add(TradingHalt(scope="portfolio", portfolio_id=pid, reason="Stop",
                          set_by="admin:admin@example.com"))
        await s.commit()
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.TRADING_HALTED
    assert BROKER.placed == []


async def test_halt_cancels_a_resting_opening_at_the_broker(maker, redis, qs):
    """I1: a broker-linked resting opening becomes cancel_requested; BrokerSync cancels it."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    sync = _sync(maker, redis, qs)
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id == "b1"
    async with maker() as s:
        _, changed, canceled = await halts.set_halt(
            s, scope="global", portfolio_id=None, reason="Stop", by="admin:admin@example.com"
        )
        await s.commit()
    assert changed and canceled == 1
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and o.broker_status == BrokerState.CANCEL_REQUESTED
    await sync.tick()
    assert BROKER.canceled == ["b1"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled


async def test_trailing_trigger_is_simulated_then_sent_to_the_broker(maker, redis, qs):
    """Ruling R10 (a)/R11: the trailing trigger stays simulated; on trigger the order
    becomes a market order (its own reduce_only flag) sent to the broker, never filled
    locally."""
    pid, link_id = await _linked_portfolio(maker)
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
    assert o.broker_link_id is None and o.status == OrderStatus.pending
    assert o.trail_ref_price is not None
    await sync.tick()
    assert len(BROKER.placed) == 1  # not sent before the trigger

    qs.set_quote("AAPL", D("90"), D("90.10"), None, ts=OPEN)
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    await proc.on_tick("AAPL", qs._quotes["AAPL"])
    o = await _order(maker, oid)
    assert (o.type, o.reduce_only, o.status) == (OrderType.market, False, OrderStatus.pending)
    assert o.broker_link_id == link_id and o.broker_status == BrokerState.TO_SEND
    assert await _fills(maker, oid) == []
    await sync.tick()
    assert BROKER.placed[-1] == ("b2", str(oid), TEN)
    BROKER.set("b2", "filled", 10, "90")
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled
    assert [f.source for f in await _fills(maker, oid)] == ["broker"]


async def _trigger_trailing_buy(maker, redis, qs, pid, **kw):
    """A trailing buy (an entry on a flat portfolio) triggered by the processor."""
    oid = await _place(
        maker, redis, qs, pid, type=OrderType.trailing_stop, side=OrderSide.buy,
        trail_pct=D(5), **kw,
    )
    assert (await _order(maker, oid)).broker_link_id is None
    qs.set_quote("AAPL", D("110"), D("110.10"), None, ts=OPEN)
    await OrderProcessor(maker, redis, qs, clock=Clock(OPEN)).on_tick(
        "AAPL", qs._quotes["AAPL"]
    )
    return oid


async def test_trailing_entry_is_routed_without_reduce_only(maker, redis, qs):
    """Ruling R11: a trailing entry keeps reduce_only False and is sent in full."""
    pid, link_id = await _linked_portfolio(maker)
    oid = await _trigger_trailing_buy(maker, redis, qs, pid)
    o = await _order(maker, oid)
    assert (o.type, o.reduce_only, o.qty) == (OrderType.market, False, TEN)
    assert o.broker_link_id == link_id and o.broker_status == BrokerState.TO_SEND
    await _sync(maker, redis, qs).tick()
    assert BROKER.placed == [("b1", str(oid), TEN)]
    BROKER.set("b1", "filled", 10, "110.10")
    await _sync(maker, redis, qs).tick()
    assert (await _order(maker, oid)).status == OrderStatus.filled
    assert len(await _trade_journals(maker, oid)) == 1


async def test_trailing_entry_under_kill_switch_is_rejected_locally(maker, redis, qs):
    """Ruling R11: the fill-time checks run before the conversion: an
    exposure-increasing trigger with the kill switch on is rejected, nothing is sent."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(
        maker, redis, qs, pid, type=OrderType.trailing_stop, side=OrderSide.buy,
        trail_pct=D(5),
    )
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    qs.set_quote("AAPL", D("110"), D("110.10"), None, ts=OPEN)
    await OrderProcessor(maker, redis, qs, clock=Clock(OPEN)).on_tick(
        "AAPL", qs._quotes["AAPL"]
    )
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected
    assert o.reject_code == str(ErrorCode.KILL_SWITCH_ACTIVE)
    assert o.broker_link_id is None and o.broker_status is None
    assert await _notes(maker, pid, "order.rejected")
    await _sync(maker, redis, qs).tick()
    assert BROKER.placed == []


async def test_reduce_only_trailing_with_nothing_to_reduce_is_canceled(maker, redis, qs):
    """Ruling R11: a reduce_only trailing trigger on a flat portfolio is canceled
    locally (nothing_to_reduce), never sent."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _trigger_trailing_buy(maker, redis, qs, pid, reduce_only=True)
    o = await _order(maker, oid)
    assert (o.status, o.reject_message) == (OrderStatus.canceled, "nothing_to_reduce")
    await _sync(maker, redis, qs).tick()
    assert BROKER.placed == []


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
    o = await _order(maker, oid)
    assert o.broker_status == BrokerState.TO_SEND and o.broker_sent_at is not None
    await sync.tick()  # looked up first: definitively not at the broker
    o = await _order(maker, oid)
    assert BROKER.finds == 1 and BROKER.placed == [] and o.broker_sent_at is None
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id == "b1"


async def test_idempotent_lost_response_is_adopted_not_resent(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    BROKER.fail_after_send = 1
    sync = _sync(maker, redis, qs)
    await sync.tick()
    await sync.tick()
    o = await _order(maker, oid)
    assert o.broker_order_id == "b1" and len(BROKER.placed) == 1


async def test_cancel_after_a_failed_send_waits_for_the_broker(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    BROKER.fail_after_send = 1
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        o = await cancel_order(s, oid, by="agent")
        await s.commit()
    assert o.status == OrderStatus.pending and o.broker_status == BrokerState.CANCEL_REQUESTED
    await sync.tick()  # found by client reference, adopted, canceled at the broker
    assert BROKER.canceled == ["b1"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled


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
    BROKER.candidates = ["t9"]
    await sync.tick()
    o = await _order(maker, oid)
    assert o.broker_order_id == "t9" and o.status == OrderStatus.filled

    oid2 = await _place(maker, redis, qs, pid)
    BROKER.fail_next, BROKER.candidates = 1, ["t9"]  # t9 already belongs to oid
    await sync.tick()
    await sync.tick()
    assert (await _order(maker, oid2)).status == OrderStatus.pending
    clock.now = OPEN + timedelta(minutes=6)
    await sync.tick()
    o2 = await _order(maker, oid2)
    assert o2.status == OrderStatus.rejected and "unknown" in o2.reject_message
    assert BROKER.placed == []


async def test_unreachable_broker_one_warning_and_the_link_skipped(maker, redis, qs, caplog):
    """An expected network failure is one WARNING line (no traceback); once a link failed
    a connection in a pass, its other orders wait for the next pass (no timeout each);
    the other links are served."""
    pid, _ = await _linked_portfolio(maker)
    other, _ = await _linked_portfolio(maker, kind="fake_other_test")
    a = await _place(maker, redis, qs, pid)
    b = await _place(maker, redis, qs, pid)
    c = await _place(maker, redis, qs, other)
    BROKER.down_kinds = {"fake_test"}
    sync = _sync(maker, redis, qs)
    with caplog.at_level(logging.WARNING, logger="smtrading.brokers.sync"):
        await sync.tick()
        assert BROKER.attempts == 2  # a (not answered) and c; b skipped
        assert (await _order(maker, a)).broker_sent_at is not None
        assert (await _order(maker, b)).broker_sent_at is None
        assert (await _order(maker, c)).broker_order_id is not None
        await sync.tick()  # a: looked up, not answered; b skipped again
        assert BROKER.finds == 1 and BROKER.attempts == 2
    mine = [r for r in caplog.records if r.name == "smtrading.brokers.sync"]
    assert mine and all(r.levelno == logging.WARNING and r.exc_info is None for r in mine)
    assert any("ConnectTimeout" in r.getMessage() and str(a) in r.getMessage()
               for r in mine)

    caplog.clear()
    BROKER.down_kinds, BROKER.find_raises = set(), ValueError("bug")
    with caplog.at_level(logging.WARNING, logger="smtrading.brokers.sync"):
        await sync.tick()  # an unexpected error keeps ERROR with its traceback
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and errors[0].exc_info is not None
    BROKER.find_raises = None
    await sync.tick()  # a: not at the broker -> to_send
    await sync.tick()
    assert (await _order(maker, a)).broker_order_id is not None
    assert (await _order(maker, b)).broker_order_id is not None


async def _system_texts(maker) -> list[str]:
    async with maker() as s:
        return list(await s.scalars(
            select(TelegramOutbox.text).where(TelegramOutbox.category == "system")
            .order_by(TelegramOutbox.id)))


async def _link(maker, link_id) -> BrokerLink:
    async with maker() as s:
        return await s.get(BrokerLink, link_id)


async def test_unreachable_paper_link_alerts_once_and_once_back(maker, redis, qs):
    """A paper/demo link that stops answering: at most one failure counted a minute
    (however many passes), one Telegram (system) at the third, nothing more while it
    lasts, one when it answers again."""
    pid, link_id = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    name = (await _link(maker, link_id)).name
    BROKER.down_kinds = {"fake_test"}
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    for _ in range(5):  # passes seconds apart: one failure
        await sync.tick()
        clock.now += timedelta(seconds=2)
    link = await _link(maker, link_id)
    assert (link.status, link.health_failures) == ("error", 1)
    assert "ConnectTimeout" in link.last_error and link.outage_since == OPEN
    clock.now = OPEN + timedelta(seconds=60)
    await sync.tick()
    await sync.tick()
    assert (await _link(maker, link_id)).health_failures == 2
    assert await _system_texts(maker) == []
    clock.now = OPEN + timedelta(seconds=120)
    await sync.tick()
    texts = await _system_texts(maker)
    assert len(texts) == 1 and name in texts[0] and "non raggiungibile" in texts[0]
    assert "ConnectTimeout" in texts[0]
    clock.now = OPEN + timedelta(seconds=200)
    assert await reconcile_all(maker, None, now=clock.now) == 0  # no second message
    assert len(await _system_texts(maker)) == 1

    BROKER.down_kinds = set()
    clock.now = OPEN + timedelta(seconds=210)
    await sync.tick()  # looked up: answered
    await sync.tick()
    await reconcile_all(maker, None, now=clock.now)
    texts = await _system_texts(maker)
    assert len(texts) == 2 and name in texts[1] and "di nuovo raggiungibile" in texts[1]
    link = await _link(maker, link_id)
    assert (link.status, link.health_failures, link.last_error) == ("ok", 0, None)
    assert (link.outage_since, link.outage_until) == (OPEN, clock.now)
    assert (await _order(maker, oid)).broker_order_id is not None


async def test_reconcile_all_failures_alert_a_link_without_orders(maker):
    _, link_id = await _linked_portfolio(maker)
    BROKER.down_kinds = {"fake_test"}
    for i in range(4):
        await reconcile_all(maker, None, now=OPEN + timedelta(minutes=5 * i))
    texts = await _system_texts(maker)
    assert len(texts) == 1 and "non raggiungibile" in texts[0]
    assert (await _link(maker, link_id)).health_failures == 4
    BROKER.down_kinds = set()
    await reconcile_all(maker, None, now=OPEN + timedelta(minutes=20))
    await reconcile_all(maker, None, now=OPEN + timedelta(minutes=25))
    texts = await _system_texts(maker)
    assert len(texts) == 2 and "di nuovo raggiungibile" in texts[1]


@pytest.mark.parametrize(("error", "counts"), [
    (RuntimeError("fake_test: HTTP 401: unauthorized"), False),
    (RuntimeError("fake_test: HTTP 500: bug"), False),
    (RuntimeError("fake_test: HTTP 503: unavailable"), True),
    (RuntimeError("fake_test: HTTP 504: gateway timeout"), True),
])
async def test_only_unanswered_reconcile_failures_count(maker, error, counts):
    """A broker that answers with a failure is not «non raggiungibile» (status error
    only); a gateway 502/503/504 is."""
    _, link_id = await _linked_portfolio(maker)
    BROKER.reconcile_raises = error
    for i in range(3):
        await reconcile_all(maker, None, now=OPEN + timedelta(minutes=5 * i))
    link = await _link(maker, link_id)
    assert link.status == "error" and str(error) in link.last_error
    assert link.health_failures == (3 if counts else 0)
    assert len(await _system_texts(maker)) == (1 if counts else 0)


async def test_a_blip_below_the_threshold_sends_nothing(maker):
    _, link_id = await _linked_portfolio(maker)
    BROKER.down_kinds = {"fake_test"}
    await reconcile_all(maker, None, now=OPEN)
    BROKER.down_kinds = set()
    await reconcile_all(maker, None, now=OPEN + timedelta(minutes=5))
    assert await _system_texts(maker) == []
    assert (await _link(maker, link_id)).status == "ok"


async def test_a_read_timeout_does_not_skip_the_link(maker, redis, qs, caplog):
    """A lookup that times out reading affects only its order: the next order of the
    link is still served (no starvation)."""
    pid, _ = await _linked_portfolio(maker)
    a = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    BROKER.fail_next = 1
    sync = _sync(maker, redis, qs)
    await sync.tick()  # a: attempted, looked up next pass
    BROKER.read_timeouts = {str(a)}
    b = await _place(maker, redis, qs, pid)
    with caplog.at_level(logging.WARNING, logger="smtrading.brokers.sync"):
        await sync.tick()
        await sync.tick()
    assert (await _order(maker, b)).broker_order_id is not None
    assert (await _order(maker, a)).broker_order_id is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_market_order_caught_by_an_outage_is_not_sent_late(maker, redis, qs):
    """Idempotent broker back after more than MATCH_WINDOW, the order confirmed absent:
    a market order is refused (PROVIDER_UNAVAILABLE, notified), never sent late; while the
    lookup is not answered nothing is decided; a limit order is sent as before."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    BROKER.down_kinds = {"fake_test"}
    await sync.tick()  # sent, not answered
    clock.now = OPEN + sync_module.MATCH_WINDOW + timedelta(seconds=1)
    await sync.tick()  # the lookup times out: ambiguous, kept
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and o.broker_sent_at is not None
    BROKER.down_kinds = set()
    await sync.tick()  # confirmed absent at the broker
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.PROVIDER_UNAVAILABLE
    assert "non inviato" in o.reject_message
    assert len(await _notes(maker, pid, "order.rejected")) == 1
    await sync.tick()
    assert BROKER.attempts == 1 and BROKER.placed == []

    start = clock.now
    lid = await _place(maker, redis, qs, pid, now=start, type=OrderType.limit,
                       limit_price=D("95"))
    BROKER.down_kinds = {"fake_test"}
    await sync.tick()
    clock.now = start + sync_module.MATCH_WINDOW + timedelta(seconds=1)
    BROKER.down_kinds = set()
    await sync.tick()  # absent: back to to_send
    await sync.tick()
    assert (await _order(maker, lid)).broker_order_id is not None


async def test_market_order_absent_within_the_window_is_sent_again(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    BROKER.down_kinds = {"fake_test"}
    await sync.tick()
    clock.now = OPEN + sync_module.MATCH_WINDOW
    BROKER.down_kinds = set()
    await sync.tick()
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id is not None


async def _created_at(maker, ids, at) -> None:
    async with maker() as s:
        await s.execute(text("UPDATE orders SET created_at = :at WHERE id = ANY(:ids)"),
                        {"at": at, "ids": list(ids)})
        await s.commit()


async def test_market_orders_queued_through_an_outage_are_refused(maker, redis, qs):
    """Never-sent market orders that waited more than MATCH_WINDOW through their link's
    outage are refused at recovery (none sent); one placed after it is sent."""
    pid, link_id = await _linked_portfolio(maker)
    ids = [await _place(maker, redis, qs, pid) for _ in range(3)]
    await _created_at(maker, ids, OPEN)  # placed on the test clock
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    BROKER.down_kinds = {"fake_test"}
    for minute in range(7):
        clock.now = OPEN + timedelta(minutes=minute)
        await sync.tick()
    BROKER.down_kinds = set()
    await sync.tick()
    await sync.tick()
    for oid in ids:
        o = await _order(maker, oid)
        assert o.status == OrderStatus.rejected, oid
        assert o.reject_code == ErrorCode.PROVIDER_UNAVAILABLE
    assert len(await _notes(maker, pid, "order.rejected")) == 3
    assert BROKER.placed == []
    link = await _link(maker, link_id)
    assert link.outage_until is not None

    clock.now += timedelta(minutes=1)
    after = await _place(maker, redis, qs, pid, now=clock.now)
    await _created_at(maker, [after], clock.now)
    await sync.tick()
    assert (await _order(maker, after)).broker_order_id is not None


async def test_market_order_queued_while_closed_is_sent_after_a_night_outage(
        maker, redis, qs):
    """An outage while the market is closed (or a healthy link) never refuses an order
    queued for the open."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY)
    await _created_at(maker, [oid], SATURDAY)
    clock = Clock(SATURDAY)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    BROKER.down_kinds = {"fake_test"}
    await reconcile_all(maker, None, now=SATURDAY)
    await reconcile_all(maker, None, now=SATURDAY + timedelta(minutes=30))
    BROKER.down_kinds = set()
    await reconcile_all(maker, None, now=SATURDAY + timedelta(minutes=35))
    clock.now = MONDAY_OPEN
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id is not None


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
    await sync.tick()  # take profit filled: the stop loss is canceled in the same pass
    assert BROKER.canceled == [sl.broker_order_id]
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled


async def test_disabled_link_rejects_unsent_orders(maker, redis, qs):
    """Ruling R10 (c): nothing falls back to the simulator on a linked portfolio."""
    pid, link_id = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        await update_link(s, link_id, enabled=False)
        await s.commit()
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and BROKER.placed == []
    assert o.reject_message == "Collegamento al broker disattivato o cambiato"
    assert len(await _notes(maker, pid, "order.rejected")) == 1


async def test_order_on_a_disabled_link_is_rejected_in_italian(maker, redis, qs):
    pid, link_id = await _linked_portfolio(maker)
    async with maker() as s:
        await update_link(s, link_id, enabled=False)
        await s.commit()
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid)
    assert e.value.message == "Collegamento al broker disattivato"


async def test_unlinked_portfolio_releases_unsent_orders_to_the_simulator(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        await link_portfolio(s, pid, None)  # explicit admin unlink, nothing in flight
        await s.commit()
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.broker_link_id is None and o.status == OrderStatus.queued
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    qs.set_quote("AAPL", D("99.90"), D("100.00"), None, ts=OPEN)
    await proc.sweep()
    assert (await _order(maker, oid)).status == OrderStatus.filled


async def test_unlink_refused_with_orders_in_flight(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    await _sync(maker, redis, qs).tick()
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await link_portfolio(s, pid, None)
    assert e.value.code == ErrorCode.INVALID_ORDER


async def test_unsupported_order_is_refused_at_placement(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid, qty=None, notional=D(500))
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.message == "Ordine non supportato dal broker collegato"
    assert BROKER.placed == []


async def test_unsupported_order_refused_with_the_adapter_reason(maker, redis, qs,
                                                                 monkeypatch):
    reason = "tipo d'ordine non disponibile su questo broker: solo quantità"
    monkeypatch.setattr(FakeBrokerAdapter, "unsupported_reason",
                        lambda self, order, instrument: reason)
    pid, _ = await _linked_portfolio(maker)
    with pytest.raises(SMTError) as e:
        await _place(maker, redis, qs, pid, qty=None, notional=D(500))
    assert (e.value.code, e.value.message) == (ErrorCode.INVALID_ORDER, reason)
    assert BROKER.placed == []


async def test_unsupported_after_prepare_is_rejected(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    BROKER.unsupported = True
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and o.reject_code == ErrorCode.INVALID_ORDER
    assert len(await _notes(maker, pid, "order.rejected")) == 1
    assert o.broker_link_id is not None and BROKER.placed == []


async def test_reconciliation_detects_a_difference(maker, redis, qs):
    pid, link_id = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    await sync.tick()
    assert (await _order(maker, oid)).status == OrderStatus.filled

    BROKER.positions = {"AAPL": D(10)}
    rows = await reconcile(maker, link_id, None)
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


# -- review fixes ----------------------------------------------------------------------


async def _set(maker, oid, **values) -> None:
    async with maker() as s:
        o = await s.get(Order, oid)
        for k, v in values.items():
            setattr(o, k, v)
        await s.commit()


async def test_find_order_never_reuses_a_linked_broker_order(maker, redis, qs):
    """C1: two identical orders; the second times out and must not be matched to the
    first one's broker order."""
    pid, _ = await _linked_portfolio(maker, kind="fake_t212_test")
    first = await _place(maker, redis, qs, pid)
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    await sync.tick()
    assert (await _order(maker, first)).broker_order_id == "b1"
    second = await _place(maker, redis, qs, pid)
    BROKER.fail_next = 1
    await sync.tick()
    BROKER.candidates = ["b1"]  # the only equal broker order is the first one's
    await sync.tick()
    assert (await _order(maker, second)).broker_order_id is None
    clock.now = OPEN + timedelta(minutes=6)
    await sync.tick()
    assert (await _order(maker, second)).status == OrderStatus.rejected


async def test_matched_order_is_checked_again_under_the_lock(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, kind="fake_t212_test")
    first = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    second = await _place(maker, redis, qs, pid)
    BROKER.fail_next = 1
    await sync.tick()
    BROKER.candidates, BROKER.ignore_exclude = ["b1"], True  # an adapter that ignores it
    await sync.tick()
    assert (await _order(maker, second)).broker_order_id is None
    assert (await _order(maker, first)).broker_order_id == "b1"
    assert await _trade_journals(maker, second) == []


async def test_broker_order_id_is_unique_per_link(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    a = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    b = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    await _set(maker, a, broker_order_id="X")
    with pytest.raises(IntegrityError):
        await _set(maker, b, broker_order_id="X")


async def _leftover_sending(maker, redis, qs, pid, *, at_broker: bool, **kw):
    """An order claimed by a worker that crashed mid-send."""
    oid = await _place(maker, redis, qs, pid, **kw)
    await _set(maker, oid, broker_status=BrokerState.SENDING, broker_sent_at=OPEN)
    if at_broker:
        BROKER.set("b7", "accepted", 0, None)
        BROKER.by_ref[str(oid)] = "b7"
    return oid


async def test_leftover_sending_on_a_disabled_link_is_adopted_then_canceled(maker, redis, qs):
    pid, link_id = await _linked_portfolio(maker)
    oid = await _leftover_sending(maker, redis, qs, pid, at_broker=True)
    async with maker() as s:
        await update_link(s, link_id, enabled=False)
        await s.commit()
    await _sync(maker, redis, qs).tick(recover=True)
    o = await _order(maker, oid)
    assert o.broker_order_id == "b7" and BROKER.canceled == ["b7"]
    assert o.status == OrderStatus.canceled and BROKER.placed == []


async def test_leftover_sending_with_kill_switch_is_adopted_then_canceled(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _leftover_sending(maker, redis, qs, pid, at_broker=True)
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await _sync(maker, redis, qs).tick(recover=True)
    o = await _order(maker, oid)
    assert o.broker_order_id == "b7" and BROKER.canceled == ["b7"]
    assert o.status == OrderStatus.canceled


async def test_leftover_sending_expires_only_after_a_definitive_not_found(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _leftover_sending(maker, redis, qs, pid, at_broker=False)
    await _set(maker, oid, expires_at=OPEN - timedelta(minutes=1))
    proc = OrderProcessor(maker, redis, qs, clock=Clock(OPEN))
    await proc.sweep()  # the processor never expires an order that may be at the broker
    assert (await _order(maker, oid)).status == OrderStatus.pending
    sync = _sync(maker, redis, qs)
    await sync.tick(recover=True)
    await sync.tick()
    o = await _order(maker, oid)
    assert BROKER.finds == 1 and o.status == OrderStatus.expired and BROKER.placed == []


async def test_leftover_sending_found_but_expired_is_canceled_at_the_broker(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _leftover_sending(maker, redis, qs, pid, at_broker=True)
    await _set(maker, oid, expires_at=OPEN - timedelta(minutes=1))
    await _sync(maker, redis, qs).tick(recover=True)
    assert BROKER.canceled == ["b7"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled


async def test_sending_is_reclaimed_only_at_startup_recovery(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _leftover_sending(maker, redis, qs, pid, at_broker=True)
    await _sync(maker, redis, qs).tick()
    assert BROKER.finds == 0 and (await _order(maker, oid)).broker_order_id is None


async def test_only_one_broker_sync_runs(maker, redis, qs, fresh_db):
    pid, _ = await _linked_portfolio(maker)
    await _place(maker, redis, qs, pid)
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        await conn.execute(
            text("select pg_advisory_lock(:k)"), {"k": sync_module.ADVISORY_LOCK_KEY}
        )
        await conn.commit()
        sync = BrokerSync(maker, redis, qs, None, clock=Clock(OPEN), interval_s=0.05)
        task = asyncio.create_task(sync.run())
        await asyncio.sleep(0.5)
        assert BROKER.placed == []  # another writer holds the lock
        await conn.execute(
            text("select pg_advisory_unlock(:k)"), {"k": sync_module.ADVISORY_LOCK_KEY}
        )
        await conn.commit()
        for _ in range(50):
            if BROKER.placed:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await engine.dispose()
    assert len(BROKER.placed) == 1


async def test_broker_sync_retakes_a_lost_lock(maker, redis, qs, fresh_db):
    """I5: when the connection holding the advisory lock dies, the passes stop until
    the lock is taken again on a new connection."""
    pid, _ = await _linked_portfolio(maker)
    sync = BrokerSync(maker, redis, qs, None, clock=Clock(OPEN), interval_s=0.05)
    task = asyncio.create_task(sync.run())
    engine = make_engine(fresh_db)
    try:
        await _place(maker, redis, qs, pid)
        for _ in range(50):
            if BROKER.placed:
                break
            await asyncio.sleep(0.1)
        assert len(BROKER.placed) == 1
        async with engine.connect() as conn:  # kill the lock holder's connection
            killed = await conn.scalar(text(
                "select count(pg_terminate_backend(pid)) from pg_locks"
                " where locktype = 'advisory' and granted"
            ))
            await conn.commit()
        assert killed == 1
        await _place(maker, redis, qs, pid)
        for _ in range(100):
            if len(BROKER.placed) == 2:
                break
            await asyncio.sleep(0.1)
        assert len(BROKER.placed) == 2
        async with engine.connect() as conn:  # held again, by a new connection
            assert await conn.scalar(text(
                "select count(*) from pg_locks where locktype = 'advisory' and granted"
            )) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.dispose()


async def test_no_quote_calls_after_a_journal(maker, redis, monkeypatch):
    """I1: a broker fill transaction never reaches the quote source while BrokerSync
    holds a portfolio lock (quotes and FX are prefetched before it), nor after its
    first journal (the chain lock is held from then until commit)."""
    state = {"journaled": False, "locked": False}

    class Counting(FakeQuoteSource):
        def __init__(self):
            super().__init__()
            self.calls, self.late = [], []

        def _rec(self, call):
            self.calls.append(call)
            if state["journaled"] or state["locked"]:
                self.late.append(call)

        async def get_quotes(self, symbols, *, max_age_s=5):
            self._rec(("quotes", tuple(symbols)))
            return await super().get_quotes(symbols, max_age_s=max_age_s)

        async def get_fx_rate(self, base, quote):
            self._rec(("fx", base, quote))
            return await super().get_fx_rate(base, quote)

    real_post, real_commit = execution_module.post_journal, AsyncSession.commit

    async def post_journal(*a, **kw):
        state["journaled"] = True
        return await real_post(*a, **kw)

    async def commit(self):
        await real_commit(self)
        state["journaled"] = state["locked"] = False

    real_lock = sync_module.lock_portfolio

    async def lock_portfolio(*a, **kw):
        state["locked"] = True
        return await real_lock(*a, **kw)

    monkeypatch.setattr(execution_module, "post_journal", post_journal)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(sync_module, "lock_portfolio", lock_portfolio)
    q = Counting()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, q, pid)
    sync = _sync(maker, redis, q)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    q.calls.clear()
    await sync.tick()
    assert (await _order(maker, oid)).status == OrderStatus.filled
    assert ("quotes", ("AAPL",)) in q.calls  # the daily loss check ran
    assert q.late == []


async def test_cancel_error_does_not_hide_fills(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        await cancel_order(s, oid, by="agent")
        await s.commit()
    BROKER.set("b1", "filled", 10, "95")  # filled before the cancel arrived
    BROKER.cancel_raises = True
    await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and len(await _trade_journals(maker, oid)) == 1


async def test_reset_waits_for_broker_cancels(maker, redis, qs):
    """I3: a reset with orders live at the broker is refused, but its cancel requests
    are kept (committed although the caller rolls back, as the admin route does); once
    BrokerSync confirms the cancels, a retry succeeds."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    await _sync(maker, redis, qs).tick()
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await reset_portfolio(s, qs, pid, D(1000), reason="test", author_id=None)
        await s.rollback()  # the route's _transaction on error
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.message == "Attendi la conferma degli annullamenti dal broker"
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending
    assert o.broker_status == BrokerState.CANCEL_REQUESTED
    await _sync(maker, redis, qs).tick()
    assert BROKER.canceled == ["b1"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled
    async with maker() as s:
        await reset_portfolio(s, qs, pid, D(1000), reason="test", author_id=None)
        await s.commit()


async def test_margin_liquidation_is_routed_to_the_broker(maker, redis):
    """Ruling R10 (b): reduce-only market orders sent to the broker, filled there in a
    LIQUIDATION journal; the portfolio stays locked meanwhile."""
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None, ts=OPEN)
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"M-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(2),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_test", {})
        await link_portfolio(s, p.id, link.id)  # before the fill: no simulated positions
        o = Order(portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy,
                  type=OrderType.market, qty=D(13000), status=OrderStatus.new)
        s.add(o)
        await s.flush()
        await execute_fill(s, q, o.id, quote=(await q.get_quotes(["ENI.MI"]))["ENI.MI"])
        await s.commit()
        pid = p.id
    q.set_quote("ENI.MI", D("8.995"), D("9.005"), None, ts=OPEN)
    async with maker() as s:
        assert await check_portfolio_margin(s, q, pid, now=OPEN) == "liquidated"
        await s.commit()
    async with maker() as s:
        liq = (await s.scalars(select(Order).where(
            Order.portfolio_id == pid, Order.role == "liquidation"
        ))).one()
        assert (liq.reduce_only, liq.type, liq.qty) == (True, OrderType.market, D(13000))
        assert liq.broker_status == BrokerState.TO_SEND and liq.status == OrderStatus.pending
        assert (await s.get(Portfolio, pid)).status == PortfolioStatus.locked
    async with maker() as s:  # no second liquidation while one is open
        await check_portfolio_margin(s, q, pid, now=OPEN)
        await s.commit()
        assert len(list(await s.scalars(
            select(Order).where(Order.portfolio_id == pid, Order.role == "liquidation")
        ))) == 1
    sync = _sync(maker, redis, q)
    await sync.tick()  # sent although the portfolio is locked
    BROKER.set("b1", "filled", 13000, "8.995")
    await sync.tick()
    async with maker() as s:
        liq = await s.get(Order, liq.id)
        assert liq.status == OrderStatus.filled
        types = list(await s.scalars(select(Journal.type).where(Journal.order_id == liq.id)))
        assert types == [JournalType.LIQUIDATION]
        assert (await s.get(Portfolio, pid)).status == PortfolioStatus.active


async def test_broker_liquidation_cancels_the_symbol_orders_first(maker, redis):
    """Concern 2 (R11): the liquidation's send first cancels, in the same pass, the
    symbol's other orders live at the broker (even when the pass reaches it first)."""
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None, ts=OPEN)
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"M-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(2),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_test", {})
        await link_portfolio(s, p.id, link.id)  # before the fill: no simulated positions
        o = Order(portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy,
                  type=OrderType.market, qty=D(13000), status=OrderStatus.new)
        s.add(o)
        await s.flush()
        await execute_fill(s, q, o.id, quote=(await q.get_quotes(["ENI.MI"]))["ENI.MI"])
        await s.commit()
        pid = p.id
    async with maker() as s:
        limit = await place_order(s, q, PlaceOrderRequest(
            portfolio_id=pid, symbol="ENI.MI", side=OrderSide.sell, type=OrderType.limit,
            qty=D(100), limit_price=D("20"),
        ), now=OPEN)
        await s.commit()
    sync = _sync(maker, redis, q)
    await sync.tick()
    assert BROKER.placed[0][1] == str(limit.id)
    q.set_quote("ENI.MI", D("8.995"), D("9.005"), None, ts=OPEN)
    async with maker() as s:
        assert await check_portfolio_margin(s, q, pid, now=OPEN) == "liquidated"
        await s.commit()
    async with maker() as s:  # the pass reaches the liquidation before the limit order
        await s.execute(text("update orders set created_at = now() + interval '1 hour'"
                             " where id = :i"), {"i": limit.id})
        await s.commit()
    placed_before = len(BROKER.placed)
    real_place = FakeBrokerAdapter.place_order
    seen = []

    async def place(self, order, instrument):
        seen.append(list(BROKER.canceled))
        return await real_place(self, order, instrument)

    FakeBrokerAdapter.place_order = place
    try:
        await sync.tick()
    finally:
        FakeBrokerAdapter.place_order = real_place
    assert len(BROKER.placed) == placed_before + 1  # the liquidation
    assert seen == [["b1"]]  # b1 was canceled before the liquidation was sent
    assert (await _order(maker, limit.id)).status == OrderStatus.canceled


async def test_liquidation_waiting_on_cancel_notifies(maker, redis):
    """Carry (T8): a liquidation stuck behind a cancel the broker refuses notifies once."""
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None, ts=OPEN)
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"M-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(2),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_test", {})
        await link_portfolio(s, p.id, link.id)
        o = Order(portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy,
                  type=OrderType.market, qty=D(13000), status=OrderStatus.new)
        s.add(o)
        await s.flush()
        await execute_fill(s, q, o.id, quote=(await q.get_quotes(["ENI.MI"]))["ENI.MI"])
        await s.commit()
        pid = p.id
    async with maker() as s:
        await place_order(s, q, PlaceOrderRequest(
            portfolio_id=pid, symbol="ENI.MI", side=OrderSide.sell, type=OrderType.limit,
            qty=D(100), limit_price=D("20"),
        ), now=OPEN)
        await s.commit()
    sync = _sync(maker, redis, q)
    await sync.tick()
    q.set_quote("ENI.MI", D("8.995"), D("9.005"), None, ts=OPEN)
    async with maker() as s:
        assert await check_portfolio_margin(s, q, pid, now=OPEN) == "liquidated"
        await s.commit()
    BROKER.cancel_raises = True
    await sync.tick()  # just created: no notification yet
    assert await _notes(maker, pid, "broker.liquidation_waiting") == []
    async with maker() as s:
        await s.execute(text("update orders set created_at = :t where role = 'liquidation'"
                             " and portfolio_id = :p"),
                        {"t": OPEN - timedelta(hours=1), "p": pid})
        await s.commit()
    await sync.tick()
    await sync.tick()
    notes = await _notes(maker, pid, "broker.liquidation_waiting")
    assert len(notes) == 1 and notes[0].payload["symbol"] == "ENI.MI"


async def test_broker_liquidation_is_clamped_after_the_symbol_orders_fill(maker, redis):
    """Re-review: a stop-loss filled at the broker before its cancel -> the liquidation
    is sent only for what is left after that fill is applied; no short is opened."""
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("ENI.MI", D("14.995"), D("15.005"), None, ts=OPEN)
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"M-{uuid.uuid4().hex[:8]}", initial_cash=D(100000), leverage=D(2),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "fake_test", {})
        await link_portfolio(s, p.id, link.id)  # before the fill: no simulated positions
        o = Order(portfolio_id=p.id, symbol="ENI.MI", side=OrderSide.buy,
                  type=OrderType.market, qty=D(13000), status=OrderStatus.new)
        s.add(o)
        await s.flush()
        await execute_fill(s, q, o.id, quote=(await q.get_quotes(["ENI.MI"]))["ENI.MI"])
        await s.commit()
        pid = p.id
    async with maker() as s:
        stop = await place_order(s, q, PlaceOrderRequest(
            portfolio_id=pid, symbol="ENI.MI", side=OrderSide.sell, type=OrderType.stop,
            qty=D(5000), stop_price=D("10"),
        ), now=OPEN)
        await s.commit()
    sync = _sync(maker, redis, q)
    await sync.tick()
    assert BROKER.placed[0][1] == str(stop.id)
    q.set_quote("ENI.MI", D("8.995"), D("9.005"), None, ts=OPEN)
    async with maker() as s:
        assert await check_portfolio_margin(s, q, pid, now=OPEN) == "liquidated"
        await s.commit()
    async with maker() as s:  # the pass reaches the liquidation before the stop
        await s.execute(text("update orders set created_at = now() + interval '1 hour'"
                             " where id = :i"), {"i": stop.id})
        await s.commit()
    BROKER.set("b1", "filled", 5000, "9.5")  # the stop filled before its cancel
    await sync.tick()
    assert (await _order(maker, stop.id)).status == OrderStatus.filled
    assert BROKER.placed[-1][2] == D(8000)  # not 13000
    async with maker() as s:
        liq = (await s.scalars(select(Order).where(
            Order.portfolio_id == pid, Order.role == "liquidation"
        ))).one()
        assert liq.qty == D(8000)
    BROKER.set("b2", "filled", 8000, "9")
    await sync.tick()
    async with maker() as s:
        bal = await s.scalar(
            select(AccountBalance.balance).join(Account).where(
                Account.portfolio_id == pid, Account.asset == "ENI.MI",
                Account.kind == AccountKind.position,
            )
        )
        assert bal == 0  # flat, no short opened
        assert (await s.get(Portfolio, pid)).status == PortfolioStatus.active


async def test_cancel_requested_never_sent_is_canceled_locally(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    await _set(maker, oid, broker_status=BrokerState.CANCEL_REQUESTED)
    await _sync(maker, redis, qs).tick()
    assert (await _order(maker, oid)).status == OrderStatus.canceled
    assert BROKER.finds == 0 and BROKER.placed == []


async def test_missing_fill_price_notifies_and_keeps_waiting(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"))
    clock = Clock(OPEN)
    sync = BrokerSync(maker, redis, qs, None, clock=clock)
    await sync.tick()
    BROKER.set("b1", "canceled", 4, None)  # canceled with fills, no price given
    await sync.tick()
    assert await _notes(maker, pid, "broker.fill_price_missing") == []
    clock.now = OPEN + timedelta(minutes=11)
    await sync.tick()
    await sync.tick()
    assert len(await _notes(maker, pid, "broker.fill_price_missing")) == 1
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and o.filled_qty == 0


def test_delta_price_not_positive_logs_a_warning(caplog):
    """Minor: an inconsistent broker average (delta price <= 0) is logged."""
    order = Order(id=uuid.uuid4(), filled_qty=D(5), avg_fill_price=D(120))
    bo = BrokerOrder(broker_order_id="b1", status="filled", filled_qty=D(10),
                     avg_fill_price=D(50), message=None)
    with caplog.at_level(logging.WARNING, logger="smtrading.brokers.sync"):
        price = sync_module.delta_price(order, bo, D(5))
    assert price == D(50)  # the broker's own average, never a negative price
    assert any("<= 0" in r.getMessage() for r in caplog.records)
    normal = sync_module.delta_price(
        Order(id=uuid.uuid4(), filled_qty=D(5), avg_fill_price=D(100)),
        BrokerOrder(broker_order_id="b1", status="filled", filled_qty=D(10),
                    avg_fill_price=D(101), message=None), D(5),
    )
    assert normal == D(102)


async def test_unknown_without_sent_at_is_treated_as_attempted(maker, redis, qs):
    """Minor (b): an `unknown` order from before broker_sent_at existed is matched
    (attempted), never left stuck nor resent."""
    pid, _ = await _linked_portfolio(maker, kind="fake_t212_test")
    oid = await _place(maker, redis, qs, pid)
    async with maker() as s:
        await s.execute(text(
            "update orders set broker_status = 'unknown', broker_sent_at = null"
            " where id = :i"), {"i": oid})
        await s.commit()
    BROKER.set("t9", "filled", 10, "100")
    BROKER.candidates = ["t9"]
    await _sync(maker, redis, qs).tick()
    o = await _order(maker, oid)
    assert o.broker_sent_at is not None and o.broker_order_id == "t9"
    assert o.status == OrderStatus.filled and BROKER.placed == []


async def test_rollback_drops_requested_followups(maker, redis, qs):
    """Minor (c): cancels requested by work that rolled back are not sent."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid)
    sync = _sync(maker, redis, qs)

    async def fails(session, order):
        sync._followups.append(order.id)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await sync._update(oid, fails)
    assert sync._followups == []


async def test_broker_sync_releases_the_lock_when_stopped(maker, redis, qs, fresh_db):
    """Minor (a): the lock connection is discarded, never pooled with the lock held."""
    sync = BrokerSync(maker, redis, qs, None, clock=Clock(OPEN), interval_s=0.05)
    task = asyncio.create_task(sync.run())
    engine = make_engine(fresh_db)
    try:
        for _ in range(50):
            async with engine.connect() as conn:
                n = await conn.scalar(text(
                    "select count(*) from pg_locks where locktype = 'advisory' and granted"))
            if n:
                break
            await asyncio.sleep(0.1)
        assert n == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with engine.connect() as conn:
            assert await conn.scalar(text(
                "select count(*) from pg_locks where locktype = 'advisory' and granted"
            )) == 0
    finally:
        await engine.dispose()


# --- eToro demo (plan 9 Task 15): the real adapter on the FakeLiveBroker transport ------

ETORO_KEYS = {"api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-0002"}


async def _etoro_portfolio(maker, cash=100000):
    async with maker() as s:
        p = await create_portfolio(
            s, name=f"P-{uuid.uuid4().hex[:8]}", initial_cash=D(cash), leverage=D(1),
        )
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "etoro_demo",
                                 dict(ETORO_KEYS))
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id, link.id


@pytest.fixture
async def etoro():
    """(fake, http client on its transport): no request ever leaves the process."""
    from smtrading.brokers.live.fake import FakeLiveBroker

    fake = FakeLiveBroker(price=D(100))
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        yield fake, http


def _etoro_sync(maker, redis, qs, http, now=OPEN):
    return BrokerSync(maker, redis, qs, http, clock=Clock(now))


def _etoro_fee(fake, fee: Decimal) -> None:
    """The broker's fee on every execution of the demo orders opened so far."""
    for record in fake.etoro["demo"].orders.values():
        for execution in record["positionExecutions"]:
            execution["openingData"]["fees"] = fee


async def test_sync_etoro_demo_buy_fill_with_broker_fee(maker, redis, qs, etoro):
    """A buy goes out to the eToro demo, is looked up (`get_order`) and filled at the
    broker's price, with the broker's fee (`BrokerOrder.fees`), not the fee rules'."""
    fake, http = etoro
    pid, _ = await _etoro_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, qty=D(2))
    sync = _etoro_sync(maker, redis, qs, http)
    await sync.tick()  # sent: the fake fills it at once
    o = await _order(maker, oid)
    assert o.broker_replicated and o.broker_order_id
    _etoro_fee(fake, D("0.42"))
    for _ in range(2):
        await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and o.filled_qty == D(2)
    assert o.avg_fill_price == D(100)
    fills = await _fills(maker, oid)
    assert [(f.qty, f.price, f.source, f.fee_amount, f.fee_currency, f.fee_rule_id)
            for f in fills] == [(D(2), D(100), "broker", D("0.42"), "USD", None)]
    paths = [p for m, p in fake.calls if m == "POST" and "/orders" in p]
    assert paths and all("/demo/" in p for p in paths)


async def test_sync_etoro_sell_composite_partial(maker, redis, qs, etoro):
    """Two buys open two eToro positions (2 + 2); a sell of 3 with no partial close
    allowed closes the first one whole: `get_order` of the composite id reports the order
    canceled with 2 filled, recorded as a partial execution."""
    fake, http = etoro
    fake.etoro_partial_close = False
    pid, _ = await _etoro_portfolio(maker)
    sync = _etoro_sync(maker, redis, qs, http)
    for _ in range(2):
        oid = await _place(maker, redis, qs, pid, qty=D(2))
        for _ in range(3):
            await sync.tick()
        assert (await _order(maker, oid)).status == OrderStatus.filled
    assert len(fake.etoro["demo"].positions) == 2

    sell = await _place(maker, redis, qs, pid, qty=D(3), side=OrderSide.sell)
    for _ in range(3):
        await sync.tick()
    o = await _order(maker, sell)
    assert o.broker_order_id.startswith("close:")
    assert o.status == OrderStatus.canceled and o.filled_qty == D(2)
    assert o.broker_status == "canceled"
    fills = await _fills(maker, sell)
    assert [(f.qty, f.price, f.source) for f in fills] == [(D(2), D(100), "broker")]
    assert [p["units"] for p in fake.etoro["demo"].positions.values()] == [D(2)]
    async with maker() as s:
        pos = await s.scalar(select(Position.qty).where(
            Position.portfolio_id == pid, Position.symbol == "AAPL"))
    assert pos == D(2)


async def _ledger_lines(maker, oid) -> list[tuple[AccountKind, str, Decimal]]:
    """(account kind, asset, quantity) of the order's journal lines."""
    from smtrading.ledger.models import JournalLine

    async with maker() as s:
        return [(k, a, D(q)) for k, a, q in (await s.execute(
            select(Account.kind, Account.asset, JournalLine.quantity)
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(Journal, Journal.id == JournalLine.journal_id)
            .where(Journal.order_id == oid)
        )).all()]


async def test_sync_etoro_demo_fee_in_account_currency_for_eur_instrument(
        maker, redis, qs, etoro, monkeypatch):
    """eToro accounts are USD-only (ACCOUNT_CURRENCY): the broker's fee on a EUR
    instrument is recorded in USD, never in the instrument's currency, and booked at the
    EUR/USD rate (no account snapshot on a demo link)."""
    from smtrading.brokers.live import fake as fake_module

    fake, http = etoro
    monkeypatch.setitem(fake_module._ETORO_INSTRUMENTS, "EXS1.DE", 2001)
    qs.set_quote("EXS1.DE", D("99.90"), D("100.00"), D("99.95"))
    pid, _ = await _etoro_portfolio(maker)
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="EXS1.DE", side=OrderSide.buy, type=OrderType.market,
            qty=D(2)), now=OPEN)
        await s.commit()
        await dispatch_post_commit(s, redis)
        oid = order.id
    sync = _etoro_sync(maker, redis, qs, http)
    await sync.tick()
    _etoro_fee(fake, D("0.50"))
    for _ in range(2):
        await sync.tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled, (o.status, o.reject_message)
    fills = await _fills(maker, oid)
    assert [(f.fee_amount, f.fee_currency, f.fee_rule_id) for f in fills] == [
        (D("0.50"), "USD", None)]
    lines = await _ledger_lines(maker, oid)
    assert [(a, q) for k, a, q in lines if k == AccountKind.system_fees] == [
        ("USD", D("0.50"))]
    # the fee bought with EUR at EUR/USD 1.25: 0.50 USD = 0.40 EUR, plus 2 x 100 EUR
    eur_cash = sum(q for k, a, q in lines if k == AccountKind.cash and a == "EUR")
    assert eur_cash == D("-200.40"), lines


async def test_etoro_rejection_text_is_agent_safe(maker, redis, qs, etoro):
    """A broker refusal whose text gives the demo away (eToro: «Demo account ...») is
    kept for humans and reads as a plain broker refusal to agents (hide mode)."""
    from smtrading.brokers.base import BROKER_REJECTED_MESSAGE
    from smtrading.mcp.dto import agent_view, order_dto, to_jsonable

    fake, _ = etoro
    text = "Demo account: insufficient virtual funds"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/demo/orders"):
            return httpx.Response(400, json={"errorMessage": text})
        return fake.transport().handle_request(request)

    pid, _ = await _etoro_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, qty=D(2))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _etoro_sync(maker, redis, qs, http).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.rejected and text in o.reject_message  # humans
    seen = agent_view(to_jsonable(order_dto(o)))
    assert seen["reject_message"] == BROKER_REJECTED_MESSAGE
    note = (await _notes(maker, pid, "order.rejected"))[0]
    assert agent_view(to_jsonable(note.payload))["message"] == BROKER_REJECTED_MESSAGE


# -- plan 16 Task A: valid_until at the broker -------------------------------------------

VALID_UNTIL = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


async def _event_types(maker, oid) -> list[str]:
    from smtrading.activity.models import Activity

    async with maker() as s:
        return list(await s.scalars(
            select(Activity.type).where(Activity.data["order_id"].astext == str(oid))
            .order_by(Activity.id)))


async def test_valid_until_sent_order_is_canceled_by_smtrading_and_expires(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       valid_until=VALID_UNTIL)
    await _sync(maker, redis, qs).tick()
    assert (await _order(maker, oid)).broker_order_id == "b1"

    early = OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL - timedelta(seconds=1)))
    await early.sweep()
    assert (await _order(maker, oid)).broker_status == "accepted"

    proc = OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL))
    await proc.sweep()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.pending and o.broker_status == BrokerState.CANCEL_REQUESTED
    await proc.sweep()  # asked once
    assert (await _event_types(maker, oid)).count("order.expired") == 0

    await _sync(maker, redis, qs, now=VALID_UNTIL + timedelta(seconds=2)).tick()
    assert BROKER.canceled == ["b1"] and BROKER.placed == [("b1", str(oid), TEN)]
    o = await _order(maker, oid)
    assert o.status == OrderStatus.expired and o.broker_status == "canceled"
    assert "order.expired" in await _event_types(maker, oid)


async def test_valid_until_partly_filled_entry_keeps_exits_for_the_filled_qty(
    maker, redis, qs,
):
    pid, _ = await _linked_portfolio(maker)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
                qty=TEN, limit_price=D("95"), valid_until=VALID_UNTIL,
            ), take_profit=D("110"), stop_loss=D("90"), now=OPEN,
        )
        await s.commit()
    later = VALID_UNTIL + timedelta(seconds=2)
    await _sync(maker, redis, qs).tick()
    BROKER.set("b1", "partially_filled", 3, "95")
    await _sync(maker, redis, qs).tick()
    assert (await _order(maker, entry.id)).filled_qty == 3

    await OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL)).sweep()
    for child in (tp, sl):  # only the entry is canceled
        assert (await _order(maker, child.id)).status == OrderStatus.held
    await _sync(maker, redis, qs, now=later).tick()
    e = await _order(maker, entry.id)
    assert e.status == OrderStatus.expired and e.filled_qty == 3
    for child in (tp, sl):
        o = await _order(maker, child.id)
        assert o.status == OrderStatus.pending and o.qty == 3 and o.valid_until is None
    await _sync(maker, redis, qs, now=later).tick()  # the exits go to the broker
    assert [q for _, _, q in BROKER.placed[1:]] == [3, 3]


async def test_valid_until_unfilled_entry_at_the_broker_cancels_its_exits(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.limit,
                qty=TEN, limit_price=D("95"), valid_until=VALID_UNTIL,
            ), take_profit=D("110"), stop_loss=D("90"), now=OPEN,
        )
        await s.commit()
    await _sync(maker, redis, qs).tick()
    await OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL)).sweep()
    await _sync(maker, redis, qs, now=VALID_UNTIL).tick()
    assert (await _order(maker, entry.id)).status == OrderStatus.expired
    for child in (tp, sl):
        assert (await _order(maker, child.id)).status == OrderStatus.canceled
    assert len(BROKER.placed) == 1


async def test_valid_until_unsent_order_expires_locally_never_sent(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, now=SATURDAY,  # queued: market closed
                       valid_until=MONDAY_OPEN - timedelta(minutes=30))
    assert (await _order(maker, oid)).broker_status == BrokerState.TO_SEND
    await OrderProcessor(maker, redis, qs, clock=Clock(MONDAY_OPEN)).sweep()
    await _sync(maker, redis, qs, now=MONDAY_OPEN).tick()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert BROKER.placed == [] and BROKER.canceled == []


async def test_valid_until_passed_is_never_sent(maker, redis, qs):
    """BrokerSync itself refuses to send past valid_until (the processor may lag)."""
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       valid_until=VALID_UNTIL)
    await _sync(maker, redis, qs, now=VALID_UNTIL).tick()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    assert BROKER.placed == []


async def test_valid_until_cancel_with_the_kill_switch_on(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       valid_until=VALID_UNTIL)
    await _sync(maker, redis, qs).tick()
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL)).sweep()
    await _sync(maker, redis, qs, now=VALID_UNTIL).tick()
    assert BROKER.canceled == ["b1"]
    assert (await _order(maker, oid)).status == OrderStatus.expired


async def test_cancel_confirmed_before_valid_until_stays_canceled(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       valid_until=VALID_UNTIL)
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        await cancel_order(s, oid, by="agent")
        await s.commit()
    await sync.tick()
    assert (await _order(maker, oid)).status == OrderStatus.canceled


async def test_valid_until_filled_at_the_broker_before_the_cancel_stands(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       valid_until=VALID_UNTIL)
    await _sync(maker, redis, qs).tick()
    await OrderProcessor(maker, redis, qs, clock=Clock(VALID_UNTIL)).sweep()
    BROKER.set("b1", "filled", 10, "95")  # filled while the cancel was on its way
    await _sync(maker, redis, qs, now=VALID_UNTIL).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.filled and o.filled_qty == 10


# -- daily renewal (Alpaca fractional quantities: day orders only) ---------------------

TUESDAY_CLOSE = datetime(2026, 11, 10, 21, 5, tzinfo=UTC)  # after the NASDAQ close
WEDNESDAY_OPEN = datetime(2026, 11, 11, 14, 31, tzinfo=UTC)
HALF = D("10.5")
_RESTING = {OrderType.limit, OrderType.stop, OrderType.stop_limit}


@registry.register
class FakeDailyAdapter(FakeBrokerAdapter):
    """Like Alpaca: a fractional gtc resting order is taken only as a day order."""

    kind: ClassVar[str] = "fake_daily_test"

    def renewed_daily(self, order, instrument) -> bool:
        return (order.tif == "gtc" and order.type in _RESTING and order.qty is not None
                and order.qty != order.qty.to_integral_value())


async def _renewal_types(maker, oid) -> list[str]:
    from smtrading.activity.models import Activity

    async with maker() as s:
        return list(await s.scalars(
            select(Activity.type).where(Activity.data["order_id"].astext == str(oid),
                                        Activity.type == "broker.order_renewed")))


async def _fractional_bracket(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, kind="fake_daily_test")
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                type=OrderType.market, qty=HALF,
            ), take_profit=D("110"), stop_loss=D("90"), now=OPEN,
        )
        await s.commit()
    sync = _sync(maker, redis, qs)
    await sync.tick()
    BROKER.set("b1", "filled", HALF, "100")
    await sync.tick()
    await sync.tick()  # exits sent
    tp, sl = await _order(maker, tp.id), await _order(maker, sl.id)
    assert tp.tif == "gtc" and sl.tif == "gtc"
    assert {tp.broker_order_id, sl.broker_order_id} == {"b2", "b3"}
    return pid, entry.id, tp, sl


async def test_fractional_day_exit_is_renewed_at_the_next_open(maker, redis, qs):
    pid, _entry, tp, sl = await _fractional_bracket(maker, redis, qs)
    # the broker expires both day exits at the close
    BROKER.set(tp.broker_order_id, "expired", 0, None)
    BROKER.set(sl.broker_order_id, "expired", 0, None)
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    for o in (await _order(maker, tp.id), await _order(maker, sl.id)):
        assert o.status == OrderStatus.pending  # still open here: gtc
        assert o.broker_status == BrokerState.TO_SEND and o.broker_order_id is None
        assert o.broker_attempt == 1 and o.broker_sent_at is None
        assert await _renewal_types(maker, o.id) == ["broker.order_renewed"]
    assert len(BROKER.placed) == 3  # nothing sent while the market is closed
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    assert len(BROKER.placed) == 3

    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    refs = sorted(ref for _, ref, _ in BROKER.placed[3:])
    assert refs == sorted([f"{tp.id}-1", f"{sl.id}-1"])  # new client references, once
    assert [q for *_, q in BROKER.placed[3:]] == [HALF, HALF]
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    assert len(BROKER.placed) == 5  # idempotent: never sent twice
    tp, sl = await _order(maker, tp.id), await _order(maker, sl.id)
    assert {tp.broker_order_id, sl.broker_order_id} == {"b4", "b5"}

    # OCO semantics kept on the renewed orders
    BROKER.set(tp.broker_order_id, "filled", HALF, "110")
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    assert BROKER.canceled == [sl.broker_order_id]
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled
    assert (await _order(maker, tp.id)).status == OrderStatus.filled
    assert await _notes(maker, pid, "order.rejected") == []


async def test_oco_sibling_fill_cancels_a_pending_renewal_locally(maker, redis, qs):
    _pid, _entry, tp, sl = await _fractional_bracket(maker, redis, qs)
    BROKER.set(sl.broker_order_id, "expired", 0, None)
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    assert (await _order(maker, sl.id)).broker_status == BrokerState.TO_SEND
    BROKER.set(tp.broker_order_id, "filled", HALF, "110")  # e.g. in the closing auction
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    assert len(BROKER.placed) == 3  # the stop is never sent again


async def test_renewal_passes_the_kill_switch_as_a_reduce_only_exit(maker, redis, qs):
    _pid, _entry, _tp, sl = await _fractional_bracket(maker, redis, qs)
    BROKER.set(sl.broker_order_id, "expired", 0, None)
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    async with maker() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    sl = await _order(maker, sl.id)
    assert sl.status == OrderStatus.pending and sl.broker_order_id is not None
    assert (f"{sl.id}-1") in [ref for _, ref, _ in BROKER.placed]


async def test_whole_or_day_orders_are_not_renewed(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, kind="fake_daily_test")
    whole = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                         tif="gtc")
    day = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       qty=HALF, tif="day")
    await _sync(maker, redis, qs).tick()
    BROKER.set("b1", "expired", 0, None)
    BROKER.set("b2", "expired", 0, None)
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    for oid in (whole, day):
        assert (await _order(maker, oid)).status == OrderStatus.expired
        assert await _renewal_types(maker, oid) == []


async def test_partly_filled_day_order_is_not_renewed_and_notifies(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, kind="fake_daily_test")
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       qty=HALF, tif="gtc")
    await _sync(maker, redis, qs).tick()
    BROKER.set("b1", "expired", 3, "95")
    await _sync(maker, redis, qs, now=TUESDAY_CLOSE).tick()
    o = await _order(maker, oid)
    assert o.status == OrderStatus.expired and o.filled_qty == 3
    assert len(await _notes(maker, pid, "broker.renewal_failed")) == 1
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    assert len(BROKER.placed) == 1


async def test_canceled_day_order_is_not_renewed(maker, redis, qs):
    pid, _ = await _linked_portfolio(maker, kind="fake_daily_test")
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D("95"),
                       qty=HALF, tif="gtc")
    sync = _sync(maker, redis, qs)
    await sync.tick()
    async with maker() as s:
        await cancel_order(s, oid, by="agent")
        await s.commit()
    BROKER.set("b1", "expired", 0, None)  # expired while the cancel was in flight
    await sync.tick()
    assert (await _order(maker, oid)).status == OrderStatus.expired
    await _sync(maker, redis, qs, now=WEDNESDAY_OPEN).tick()
    assert len(BROKER.placed) == 1


# -- piani and closes on a broker-linked portfolio (plan-close review) -------------------


async def _held_at_broker(maker, redis, qs, sync) -> uuid.UUID:
    """A linked portfolio long 10 AAPL, filled at the broker (b1)."""
    pid, _ = await _linked_portfolio(maker)
    await _place(maker, redis, qs, pid)
    await sync.tick()
    BROKER.set("b1", "filled", 10, "100")
    await sync.tick()
    return pid


async def _plan_row(maker, pid, symbol="AAPL") -> None:
    from datetime import date

    from smtrading.goals.models import Goal, PlanClosure

    async with maker() as s:
        held = await s.get(Position, (pid, symbol))
        g = Goal(name=f"piano-{uuid.uuid4().hex[:6]}", horizon="week", recurring=True,
                 scope="portfolio", portfolio_id=pid, kind="min", target_pct=D(2),
                 active=True, closes_positions=True,
                 plan_since=datetime(2026, 11, 1, tzinfo=UTC), start_date=date(2026, 11, 2))
        s.add(g)
        await s.flush()
        s.add(PlanClosure(
            goal_id=g.id, period_start=date(2026, 11, 2), period_end=date(2026, 11, 8),
            ended_at=datetime(2026, 11, 8, 23, 0, tzinfo=UTC), portfolio_id=pid,
            symbol=symbol, qty=TEN, opened_at=held.opened_at, state="pending", grace_days=0,
            attempts=0))
        await s.commit()


async def test_an_addition_in_plan_closing_is_never_sent(maker, redis, qs):
    sync = _sync(maker, redis, qs)
    pid = await _held_at_broker(maker, redis, qs, sync)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, qty=D(5),
                       limit_price=D("95"))  # placed before the end, not sent yet
    await _plan_row(maker, pid)
    await sync.tick()
    o = await _order(maker, oid)
    assert (o.status, o.reject_code) == (OrderStatus.rejected, ErrorCode.RISK_LIMIT_EXCEEDED)
    assert "posizione in chiusura per fine piano" in o.reject_message
    assert [p[0] for p in BROKER.placed] == ["b1"]


async def test_an_addition_in_plan_closing_at_the_broker_is_canceled(maker, redis, qs):
    from smtrading.activity.models import Activity

    sync = _sync(maker, redis, qs)
    pid = await _held_at_broker(maker, redis, qs, sync)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, qty=D(5),
                       limit_price=D("95"))
    await sync.tick()
    assert (await _order(maker, oid)).broker_order_id == "b2"
    await _plan_row(maker, pid)
    await sync.tick()  # read, cancel requested, canceled in the same pass
    assert BROKER.canceled == ["b2"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled
    async with maker() as s:
        act = await s.scalar(select(Activity).where(Activity.type == "plan.orders_canceled"))
    assert act.portfolio_id == pid and act.summary.startswith("AAPL: annullato 1 ordine")


async def test_the_plan_closer_cancels_an_addition_at_the_broker(maker, redis, qs):
    from smtrading.goals.plan_closer import PlanCloser

    sync = _sync(maker, redis, qs)
    pid = await _held_at_broker(maker, redis, qs, sync)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, qty=D(5),
                       limit_price=D("95"))
    await sync.tick()
    await _plan_row(maker, pid)
    await PlanCloser(maker, qs, redis, clock=Clock(SATURDAY), tz="Europe/Rome").tick()
    o = await _order(maker, oid)
    assert (o.status, o.broker_status) == (OrderStatus.pending, BrokerState.CANCEL_REQUESTED)
    await sync.tick()
    assert BROKER.canceled == ["b2"]
    assert (await _order(maker, oid)).status == OrderStatus.canceled


async def test_full_close_cancels_a_standalone_stop_at_the_broker(maker, redis, qs):
    """CHIUDI on a linked portfolio: the stop placed alone (not reduce-only) is canceled
    at the broker, so it neither holds the quantity nor opens a short once flat."""
    sync = _sync(maker, redis, qs)
    pid = await _held_at_broker(maker, redis, qs, sync)
    stop = await _place(maker, redis, qs, pid, type=OrderType.stop, side=OrderSide.sell,
                        stop_price=D("90"))
    await sync.tick()
    assert (await _order(maker, stop)).broker_order_id == "b2"
    async with maker() as s:
        close = await close_position(s, qs, pid, "AAPL", now=OPEN)
        await s.commit()
        await dispatch_post_commit(s, redis)
    assert (await _order(maker, stop)).broker_status == BrokerState.CANCEL_REQUESTED
    await sync.tick()
    assert "b2" in BROKER.canceled
    assert (await _order(maker, stop)).status == OrderStatus.canceled
    assert (await _order(maker, close.id)).broker_order_id == "b3"
