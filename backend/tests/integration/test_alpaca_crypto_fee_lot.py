"""Alpaca crypto: the coin fee leaves a held quantity finer than the lot (live paper
drill, 26/11: 200 $ of ETH, held 0.115188078 against a lot of 0.00000001, every exit
refused «quantity is not a multiple of 0.00000001»).

The real AlpacaPaperAdapter on the FakeLiveBroker transport. 200 $ of ETH-USD at 1735:
0.11527377 ordered (floored to the lot), the 0.25 % taker fee kept in the coin rounded
up to 1e-9 (0.000288185): 0.114985585 credited here, 9 decimals. Every exit derived
from it (bracket stop and target, a close of «all») is floored to the lot, accepted and
filled; the remainder below one lot goes with it (closed here, its value as fee).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import BrokerState, Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    place_bracket_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.instruments.service import floor_to_lot
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.seed import run_seed

D = Decimal
SYM, PAIR = "ETH-USD", "ETH/USD"
NOW = datetime(2026, 11, 21, 20, 48, tzinfo=UTC)  # a Saturday: crypto trades
PRICE = D(1735)
LOT = D("0.00000001")
QTY = D("0.11527377")  # 200 $ at 1735, floored to the lot
NET = D("0.114985585")  # less the coin fee (0.000288185, rounded up to 1e-9): 9 decimals
EXIT = D("0.11498558")  # NET floored to the lot
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}


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
    q.set_quote(SYM, PRICE - 1, PRICE, PRICE)
    return q


@pytest.fixture
async def alpaca():
    fake = FakeLiveBroker(price=PRICE)
    fake.alpaca.cash = D(100000)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        yield fake, http


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def _portfolio(maker) -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", "alpaca_paper", dict(CFG))
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id


def _req(pid, **kw) -> PlaceOrderRequest:
    return PlaceOrderRequest(**{"portfolio_id": pid, "symbol": SYM, "side": OrderSide.buy,
                                "type": OrderType.market, "qty": QTY, **kw})


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


async def _held(maker, pid) -> tuple[Decimal, Decimal]:
    async with maker() as s:
        ledger = await s.scalar(
            select(AccountBalance.balance)
            .join(Account, AccountBalance.account_id == Account.id)
            .where(Account.portfolio_id == pid, Account.asset == SYM,
                   Account.kind == AccountKind.position)) or D(0)
        row = await s.scalar(select(Position.qty).where(
            Position.portfolio_id == pid, Position.symbol == SYM)) or D(0)
        return ledger, row


async def _settle(maker, redis, qs, http, n: int = 3) -> None:
    sync = BrokerSync(maker, redis, qs, http, clock=Clock(NOW))
    for _ in range(n):
        await sync.tick()


def test_floor_to_lot():
    assert floor_to_lot(NET, LOT) == EXIT
    assert floor_to_lot(D("0.115188078"), LOT) == D("0.11518807")
    assert floor_to_lot(D("0.000000009"), LOT) == 0  # dust: below one lot
    assert floor_to_lot(D(5), D(1)) == D(5)  # already a multiple: unchanged
    assert floor_to_lot(D("4.35"), D("0.000000001")) == D("4.35")
    assert floor_to_lot(D("4.9"), D(1)) == D(4)  # never up


async def test_bracket_children_are_floored_to_the_lot_and_the_stop_fills(
        maker, redis, qs, alpaca):
    fake, http = alpaca
    pid = await _portfolio(maker)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, _req(pid), take_profit=D(1900), stop_loss=D(1600), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(maker, redis, qs, http)
    assert (await _order(maker, entry.id)).status == OrderStatus.filled
    assert await _held(maker, pid) == (NET, NET)
    for child in (tp, sl):
        c = await _order(maker, child.id)
        assert (c.status, c.qty) == (OrderStatus.pending, EXIT)

    fake.price = D(1599)
    qs.set_quote(SYM, D(1599), D(1600), None, ts=NOW)
    assert await OrderProcessor(maker, redis, qs, clock=Clock(NOW)).on_tick(
        SYM, qs._quotes[SYM]) == 1
    assert (await _order(maker, sl.id)).broker_status == BrokerState.TO_SEND
    await _settle(maker, redis, qs, http)
    s_ = await _order(maker, sl.id)
    assert (s_.status, s_.filled_qty, s_.reject_message) == (OrderStatus.filled, EXIT, None)
    assert (await _order(maker, tp.id)).status == OrderStatus.canceled
    assert await _held(maker, pid) == (0, 0)  # the remainder below one lot went with it


async def test_close_all_sells_the_lot_floored_quantity(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid = await _portfolio(maker)
    async with maker() as s:
        buy = await place_order(s, qs, _req(pid), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(maker, redis, qs, http)
    assert (await _order(maker, buy.id)).status == OrderStatus.filled
    assert await _held(maker, pid) == (NET, NET)

    async with maker() as s:
        close = await close_position(s, qs, pid, SYM, now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    assert close.qty == EXIT and close.status != OrderStatus.rejected
    await _settle(maker, redis, qs, http)
    o = await _order(maker, close.id)
    assert (o.status, o.filled_qty) == (OrderStatus.filled, EXIT)
    assert fake.alpaca.orders[o.broker_order_id]["qty"] == "0.11498558"
    async with maker() as s:
        [f] = list(await s.scalars(select(Fill).where(Fill.order_id == close.id)))
    assert f.qty == NET  # the remainder below one lot closed here with the sell
    assert await _held(maker, pid) == (0, 0)


async def test_a_sell_of_the_ordered_quantity_is_sized_to_the_lot_floored_position(
        maker, redis, qs, alpaca):
    """A sell of the quantity ordered on the entry (beyond the position by the coin fee)
    is the position, floored to the lot: accepted, never a short."""
    _, http = alpaca
    pid = await _portfolio(maker)
    async with maker() as s:
        await place_order(s, qs, _req(pid), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(maker, redis, qs, http)
    async with maker() as s:
        sell = await place_order(s, qs, _req(pid, side=OrderSide.sell), now=NOW)
        await s.commit()
    assert sell.qty == EXIT and sell.status != OrderStatus.rejected


async def test_a_sell_of_exactly_the_held_quantity_is_floored_to_the_lot(
        maker, redis, qs, alpaca):
    """An agent selling the 9-decimal position as it reads it (MCP place_order): the
    position floored to the lot, accepted; a lot multiple is left as it is."""
    _, http = alpaca
    pid = await _portfolio(maker)
    async with maker() as s:
        await place_order(s, qs, _req(pid), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(maker, redis, qs, http)
    async with maker() as s:
        sell = await place_order(s, qs, _req(pid, side=OrderSide.sell, qty=NET), now=NOW)
        await s.commit()
    assert sell.qty == EXIT and sell.status != OrderStatus.rejected
    async with maker() as s:
        part = await place_order(s, qs, _req(pid, side=OrderSide.sell, qty=D("0.00001"),
                                             client_order_id="part"), now=NOW)
        await s.commit()
    assert part.qty == D("0.00001")
