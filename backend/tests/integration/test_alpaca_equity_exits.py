"""Alpaca equities: the two exits of a bracket / OCO never rest together at the broker
(plan 47, found live on 23/11 on MSFT 4.35 shares).

Alpaca holds the shares of a position for its first resting sell: a second sell of the
full position is refused («insufficient qty available for order (requested: 4.35,
available: 0)»), and a native OCO / bracket does not take fractional quantities. So both
exits of an OCO group (bracket exits included) are kept and watched by SMTrading and sent
at market at their trigger, like Alpaca crypto (plan 37). A stop placed alone still rests
at Alpaca. The real AlpacaPaperAdapter on the FakeLiveBroker transport, which models the
share reservation and the fractional refusal of the native order classes.
"""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync, reconcile
from smtrading.common.db import make_engine
from smtrading.engine.models import (
    BrokerState,
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
    cancel_order,
    place_bracket_order,
    place_oco_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
SYM = "MSFT"
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday 10:00 New York: NASDAQ open


@pytest.fixture(autouse=True)
def _frozen_risk_clock(monkeypatch):
    """The risk check reads the wall clock for a quote's age; the quotes here are stamped
    NOW (a fixed Monday morning), so the check must see NOW too (else the test breaks
    two minutes after that time)."""
    import smtrading.risk.checks as risk_checks

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(risk_checks, "datetime", _Frozen)
QTY = D("4.35")
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}
REFUSED = "insufficient qty available for order (requested: 4.35, available: 0)"


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
    q.set_quote(SYM, D("502.90"), D("503.00"), D("502.95"), ts=NOW)
    return q


@pytest.fixture
async def alpaca():
    """(fake, http client on its transport): no request ever leaves the process."""
    fake = FakeLiveBroker(price=D("503.00"))
    fake.alpaca.cash = D(100000)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        yield fake, http


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _inst(symbol=SYM, ac=AssetClass.us_equity) -> Instrument:
    return Instrument(symbol=symbol, name=symbol, asset_class=ac, market="NASDAQ",
                      currency="USD")


def _order(**kw) -> Order:
    base = {"id": uuid.uuid4(), "symbol": SYM, "side": OrderSide.sell,
            "type": OrderType.stop, "tif": TimeInForce.gtc, "qty": QTY,
            "stop_price": D(478), "filled_qty": D(0)}
    base.update(kw)
    return Order(**base)


async def _portfolio(maker) -> tuple[uuid.UUID, uuid.UUID]:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper", dict(CFG))
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id, link.id


def _req(pid, **kw) -> PlaceOrderRequest:
    return PlaceOrderRequest(**{"portfolio_id": pid, "symbol": SYM, "side": OrderSide.buy,
                                "type": OrderType.market, "qty": QTY, **kw})


async def _place(maker, redis, qs, pid, **kw) -> uuid.UUID:
    async with maker() as s:
        order = await place_order(s, qs, _req(pid, **kw), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order_row(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


def _sync(maker, redis, qs, http) -> BrokerSync:
    return BrokerSync(maker, redis, qs, http, clock=Clock(NOW))


async def _settle(sync: BrokerSync, n: int = 3) -> None:
    for _ in range(n):
        await sync.tick()


async def _tick(maker, redis, qs, bid, ask) -> int:
    qs.set_quote(SYM, D(bid), D(ask), None, ts=NOW)
    return await OrderProcessor(maker, redis, qs, clock=Clock(NOW)).on_tick(
        SYM, qs._quotes[SYM])


def _posted(fake) -> list[dict]:
    return [o for o in fake.alpaca.orders.values()]


async def _bracket(maker, redis, qs, pid, qty=QTY):
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, _req(pid, type=OrderType.limit, limit_price=D("503.50"), qty=qty,
                        tif=TimeInForce.gtc),
            take_profit=D("533.71"), stop_loss=D("478.33"), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    return entry, tp, sl


async def _reconciled_ok(maker, link_id, http) -> bool:
    rows = await reconcile(maker, link_id, http, notify=False)
    return all(r.ok for r in rows)


# -- the fake broker models what Alpaca answered ---------------------------------------

async def test_fake_holds_the_shares_for_the_first_resting_sell(alpaca):
    """The live case of 23/11: a stop for the whole position, then a limit for the whole
    position: the second is refused with Alpaca's text."""
    fake, http = alpaca
    fake.alpaca.positions[SYM] = QTY
    adapter = AlpacaPaperAdapter(CFG, http)
    stop = await adapter.place_order(_order(), _inst())
    assert stop.status == "accepted"
    tp = await adapter.place_order(
        _order(type=OrderType.limit, stop_price=None, limit_price=D("533.71")), _inst())
    assert (tp.status, tp.message) == ("rejected", REFUSED)
    market = await adapter.place_order(_order(type=OrderType.market, stop_price=None,
                                              tif=TimeInForce.day), _inst())
    assert (market.status, market.message) == ("rejected", REFUSED)  # held by the stop
    await adapter.cancel_order(stop.broker_order_id)
    market = await adapter.place_order(_order(type=OrderType.market, stop_price=None,
                                              tif=TimeInForce.day), _inst())
    assert market.status == "filled" and fake.alpaca.positions == {}


@pytest.mark.parametrize("order_class", ["oco", "bracket", "oto"])
async def test_fake_refuses_a_fractional_native_order_class(alpaca, order_class):
    fake, http = alpaca
    fake.alpaca.positions[SYM] = QTY
    body = {"symbol": SYM, "qty": "4.35", "side": "sell", "type": "limit",
            "time_in_force": "day", "limit_price": "533.71", "order_class": order_class,
            "take_profit": {"limit_price": "533.71"}, "stop_loss": {"stop_price": "478.33"},
            "client_order_id": "c1"}
    resp = await http.post("https://paper-api.alpaca.markets/v2/orders", content=json.dumps(body))
    assert resp.status_code == 422
    assert resp.json()["message"] == "fractional orders must be simple orders"


# -- the routing rule --------------------------------------------------------------------

@pytest.mark.parametrize("cls", [AlpacaPaperAdapter, AlpacaLiveAdapter])
@pytest.mark.parametrize("qty", [QTY, D(4)])
def test_oco_legs_on_equities_are_kept_here_a_lone_stop_rests_at_alpaca(cls, qty):
    a = cls.__new__(cls)  # routing needs no network nor configuration
    a.crypto_enabled = False
    group = uuid.uuid4()
    for leg in (_order(qty=qty, oco_group_id=group),
                _order(qty=qty, oco_group_id=group, type=OrderType.limit, stop_price=None,
                       limit_price=D("533.71")),
                _order(qty=qty, oco_group_id=group, type=OrderType.stop_limit,
                       limit_price=D(470)),
                _order(qty=qty, oco_group_id=group, type=OrderType.trailing_stop,
                       stop_price=None, trail_pct=D(5))):
        assert not a.routable(leg, _inst()) and a.synthetic(leg, _inst())
    lone = _order(qty=qty)
    assert a.routable(lone, _inst()) and not a.synthetic(lone, _inst())
    limit = _order(qty=qty, type=OrderType.limit, stop_price=None, limit_price=D(533))
    assert a.routable(limit, _inst()) and not a.synthetic(limit, _inst())


# -- brackets ------------------------------------------------------------------------------

@pytest.mark.parametrize("qty", [QTY, D(4)])
async def test_bracket_exits_are_kept_here_and_the_take_profit_sells_at_market(
        maker, redis, qs, alpaca, qty):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    entry, tp, sl = await _bracket(maker, redis, qs, pid, qty)
    await _settle(sync)
    e = await _order_row(maker, entry.id)
    assert e.broker_order_id and not e.synthetic  # the entry rests at Alpaca
    fake.alpaca_fill(e.broker_order_id, D("503.02"))
    await _settle(sync)
    assert (await _order_row(maker, entry.id)).status == OrderStatus.filled
    for child in (tp, sl):  # never two resting sells at Alpaca: both kept here
        c = await _order_row(maker, child.id)
        assert (c.status, c.synthetic, c.broker_link_id, c.reject_message) == (
            OrderStatus.pending, True, None, None)
    assert len(_posted(fake)) == 1
    assert await _reconciled_ok(maker, link_id, http)

    fake.price = D("533.80")
    assert await _tick(maker, redis, qs, "533.80", "533.90") == 1
    assert (await _order_row(maker, tp.id)).broker_status == BrokerState.TO_SEND
    await _settle(sync)
    t = await _order_row(maker, tp.id)
    assert (t.status, t.type, t.filled_qty, t.avg_fill_price) == (
        OrderStatus.filled, OrderType.limit, qty, D("533.80"))
    sent = fake.alpaca.orders[t.broker_order_id]
    assert (sent["type"], sent["side"], sent["qty"]) == ("market", "sell", format(qty, "f"))
    assert (await _order_row(maker, sl.id)).status == OrderStatus.canceled
    assert fake.alpaca.positions == {}
    assert await _reconciled_ok(maker, link_id, http)


async def test_bracket_stop_sells_everything_and_cancels_the_target(
        maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    async with maker() as s:  # a market entry, as most agents place it
        entry, tp, sl = await place_bracket_order(
            s, qs, _req(pid), take_profit=D("533.71"), stop_loss=D("478.33"), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(sync)
    assert (await _order_row(maker, entry.id)).status == OrderStatus.filled
    fake.price = D("478.00")
    assert await _tick(maker, redis, qs, "478.00", "478.10") == 1
    await _settle(sync)
    s_ = await _order_row(maker, sl.id)
    assert (s_.status, s_.type, s_.filled_qty) == (OrderStatus.filled, OrderType.stop, QTY)
    assert (await _order_row(maker, tp.id)).status == OrderStatus.canceled
    assert fake.alpaca.positions == {}
    assert not any(o["status"] == "rejected" for o in fake.alpaca.orders.values())
    assert await _reconciled_ok(maker, link_id, http)


# -- OCO placed on a position ---------------------------------------------------------------

async def _buy(maker, redis, qs, pid, sync) -> None:
    oid = await _place(maker, redis, qs, pid)
    await _settle(sync)
    assert (await _order_row(maker, oid)).status == OrderStatus.filled


async def _oco(maker, qs, pid) -> list[Order]:
    async with maker() as s:
        legs = await place_oco_order(
            s, qs,
            _req(pid, side=OrderSide.sell, type=OrderType.limit, limit_price=D("533.71"),
                 tif=TimeInForce.gtc),
            _req(pid, side=OrderSide.sell, type=OrderType.stop, stop_price=D("478.33"),
                 tif=TimeInForce.gtc),
            now=NOW)
        await s.commit()
    return legs


async def test_oco_legs_on_equities_are_both_kept_here(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    legs = await _oco(maker, qs, pid)
    await _settle(sync)
    for leg in legs:
        o = await _order_row(maker, leg.id)
        assert (o.status, o.synthetic, o.broker_link_id) == (OrderStatus.pending, True, None)
    assert len(_posted(fake)) == 1  # the buy only
    assert await _reconciled_ok(maker, link_id, http)


async def test_recovery_a_lone_stop_at_alpaca_then_an_oco_never_double_sells(
        maker, redis, qs, alpaca):
    """The MSFT position of 23/11: its stop rests at Alpaca, its target was refused. The
    OCO placed next is kept here; while the old stop holds the shares at Alpaca a
    triggered leg waits (never sent, never refused); once that stop is canceled the
    OCO is the position's protection and its target sells at market."""
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    old = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                       stop_price=D("478.33"), tif=TimeInForce.gtc)
    await _settle(sync)
    o = await _order_row(maker, old)
    assert o.broker_order_id and not o.synthetic  # a lone stop rests at Alpaca
    tp, sl = await _oco(maker, qs, pid)
    await _settle(sync)
    for x in (tp, sl):
        assert (await _order_row(maker, x.id)).synthetic

    fake.price = D("533.80")
    await _tick(maker, redis, qs, "533.80", "533.90")
    await _settle(sync)
    t = await _order_row(maker, tp.id)
    assert t.status in (OrderStatus.pending, OrderStatus.triggered)
    assert t.filled_qty == 0 and t.reject_message is None
    assert not any(x["status"] == "rejected" for x in fake.alpaca.orders.values())
    assert fake.alpaca.positions == {SYM: QTY}

    async with maker() as s:
        await cancel_order(s, old, by="agent")
        await s.commit()
    await _settle(sync)
    assert (await _order_row(maker, old)).status == OrderStatus.canceled
    await _tick(maker, redis, qs, "533.80", "533.90")
    await _settle(sync)
    t = await _order_row(maker, tp.id)
    assert (t.status, t.filled_qty) == (OrderStatus.filled, QTY)
    assert (await _order_row(maker, sl.id)).status == OrderStatus.canceled
    assert fake.alpaca.positions == {}
    assert await _reconciled_ok(maker, link_id, http)
