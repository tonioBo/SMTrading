"""Alpaca crypto: the fee kept in the coin on a buy, in USD on a sell (plan 37).

The real AlpacaPaperAdapter on the FakeLiveBroker transport, whose Alpaca crypto answers
follow the paper API's shapes (read on 21/11: filled 0.0015 BTC/USD, the account then
holds 0.00149625; the order entity carries no fee). A buy books the net quantity and the
fee's value; the exits (bracket, a stop placed after the entry, a close) sell what the
account holds; nothing is left behind here or at the broker; the reconciliation is ok.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync, reconcile
from smtrading.common.db import make_engine
from smtrading.common.money import quantize_money
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
    close_position,
    place_bracket_order,
    place_oco_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.reconciliation.compare import compare_positions
from smtrading.seed import run_seed

D = Decimal
SYM, PAIR = "BTC-USD", "BTC/USD"
NOW = datetime(2026, 11, 21, 20, 48, tzinfo=UTC)  # a Saturday: crypto trades
PRICE = D(60000)
QTY = D("0.0015")
NET = D("0.00149625")  # QTY less the taker fee of tier 1 (0.25 %), kept in the coin
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
    q.set_quote(SYM, PRICE - 5, PRICE + 5, PRICE)
    return q


@pytest.fixture
async def alpaca():
    """(fake, http client on its transport): no request ever leaves the process."""
    fake = FakeLiveBroker(price=PRICE)
    fake.alpaca.cash = D(100000)
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


async def _close(maker, redis, qs, pid) -> uuid.UUID:
    async with maker() as s:
        order = await close_position(s, qs, pid, SYM, now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
        return order.id


async def _order(maker, oid) -> Order:
    async with maker() as s:
        return await s.get(Order, oid)


async def _fills(maker, oid) -> list[Fill]:
    async with maker() as s:
        return list(await s.scalars(select(Fill).where(Fill.order_id == oid)))


async def _held(maker, pid) -> tuple[Decimal, Decimal]:
    """(ledger position, positions row) of BTC-USD."""
    async with maker() as s:
        ledger = await s.scalar(
            select(AccountBalance.balance)
            .join(Account, AccountBalance.account_id == Account.id)
            .where(Account.portfolio_id == pid, Account.asset == SYM,
                   Account.kind == AccountKind.position)) or D(0)
        row = await s.scalar(select(Position.qty).where(
            Position.portfolio_id == pid, Position.symbol == SYM)) or D(0)
        return ledger, row


def _sync(maker, redis, qs, http) -> BrokerSync:
    return BrokerSync(maker, redis, qs, http, clock=Clock(NOW))


async def _settle(sync: BrokerSync, n: int = 3) -> None:
    for _ in range(n):
        await sync.tick()


async def _tick(maker, redis, qs, bid, ask) -> int:
    qs.set_quote(SYM, D(bid), D(ask), None, ts=NOW)
    done = await OrderProcessor(maker, redis, qs, clock=Clock(NOW)).on_tick(
        SYM, qs._quotes[SYM])
    qs.set_quote(SYM, D(bid), D(ask), None)
    return done


def _posted(fake) -> list[str]:
    return [p for m, p in fake.calls if m == "POST" and p == "/v2/orders"]


async def _buy(maker, redis, qs, pid, sync) -> uuid.UUID:
    oid = await _place(maker, redis, qs, pid)
    await _settle(sync)
    assert (await _order(maker, oid)).status == OrderStatus.filled
    return oid


async def _reconciled_ok(maker, link_id, http) -> bool:
    rows = await reconcile(maker, link_id, http, notify=False)
    return all(r.ok for r in rows)


# -- buy: the net quantity and the fee -----------------------------------------------------

async def test_buy_books_the_net_quantity_and_the_coin_fee(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    oid = await _buy(maker, redis, qs, pid, sync)

    assert fake.alpaca.positions == {PAIR: NET}  # the paper API's answer on 21/11
    o = await _order(maker, oid)
    assert (o.filled_qty, o.avg_fill_price, o.broker_status) == (QTY, PRICE, "filled")
    [f] = await _fills(maker, oid)
    assert (f.qty, f.price, f.source, f.fee_currency, f.fee_rule_id) == (
        NET, PRICE, "broker", "USD", None)
    assert f.fee_amount == quantize_money((QTY - NET) * PRICE, "USD")  # 0.23 USD
    assert await _held(maker, pid) == (NET, NET)
    assert await _reconciled_ok(maker, link_id, http)


async def test_resting_buy_limit_pays_the_maker_fee(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    oid = await _place(maker, redis, qs, pid, type=OrderType.limit, limit_price=D(59000))
    await _settle(sync)
    o = await _order(maker, oid)
    assert o.broker_order_id and not o.synthetic  # a buy limit rests at Alpaca
    fake.alpaca_fill(o.broker_order_id, D(59000))
    # it rested: filled an hour after its submission (a date relative to the run, not fixed)
    sub = datetime.fromisoformat(fake.alpaca.orders[o.broker_order_id]["submitted_at"])
    fake.alpaca.orders[o.broker_order_id]["filled_at"] = (
        (sub + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")
    await _settle(sync)
    net = QTY - QTY * D("0.0015")  # maker, tier 1
    assert fake.alpaca.positions == {PAIR: net}
    [f] = await _fills(maker, oid)
    assert (f.qty, f.price) == (net, D(59000))
    assert f.fee_amount == quantize_money(QTY * D("0.0015") * D(59000), "USD")


# -- exits: what the account holds ---------------------------------------------------------

async def test_bracket_exits_are_kept_here_net_sized_and_the_stop_sells_everything(
        maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    async with maker() as s:
        entry, tp, sl = await place_bracket_order(
            s, qs, _req(pid), take_profit=D(66000), stop_loss=D(54000), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    await _settle(sync)
    assert (await _order(maker, entry.id)).status == OrderStatus.filled
    for child in (tp, sl):  # no OCO at Alpaca: both kept here, sized to the coins held
        c = await _order(maker, child.id)
        assert (c.status, c.synthetic, c.broker_link_id, c.qty) == (
            OrderStatus.pending, True, None, NET)
    assert len(_posted(fake)) == 1

    fake.price = D(53990)
    assert await _tick(maker, redis, qs, "53990", "54000") == 1
    assert (await _order(maker, sl.id)).broker_status == BrokerState.TO_SEND
    await _settle(sync)
    s_ = await _order(maker, sl.id)
    assert (s_.status, s_.type, s_.filled_qty) == (OrderStatus.filled, OrderType.stop, NET)
    assert (await _order(maker, tp.id)).status == OrderStatus.canceled
    [f] = await _fills(maker, sl.id)
    assert f.fee_amount == quantize_money(NET * D(53990) * D("0.0025"), "USD")
    assert f.fee_currency == "USD"
    assert fake.alpaca.positions == {}
    assert await _held(maker, pid) == (0, 0)
    assert await _reconciled_ok(maker, link_id, http)


async def test_a_stop_placed_for_the_ordered_qty_right_after_the_entry_sells_the_net(
        maker, redis, qs, alpaca):
    """A plain stop on crypto (Alpaca has none) is kept here, not refused; a sell of the
    quantity ordered on the entry is the position (net of the coin fee), never a short."""
    fake, http = alpaca
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    stop = await _place(maker, redis, qs, pid, side=OrderSide.sell, type=OrderType.stop,
                        stop_price=D(55000))
    o = await _order(maker, stop)
    assert (o.status, o.synthetic, o.reject_message, o.qty) == (
        OrderStatus.pending, True, None, NET)
    fake.price = D(54990)
    assert await _tick(maker, redis, qs, "54990", "55000") == 1
    await _settle(sync)
    o = await _order(maker, stop)
    assert (o.status, o.filled_qty) == (OrderStatus.filled, NET)
    assert fake.alpaca.positions == {}
    assert await _held(maker, pid) == (0, 0)


async def test_oco_legs_on_crypto_are_both_kept_here(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, _ = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    async with maker() as s:
        legs = await place_oco_order(
            s, qs,
            _req(pid, side=OrderSide.sell, type=OrderType.limit, qty=NET,
                 limit_price=D(66000)),
            _req(pid, side=OrderSide.sell, type=OrderType.stop, qty=NET,
                 stop_price=D(54000)),
            now=NOW)
        await s.commit()
    await _settle(sync)
    for leg in legs:
        o = await _order(maker, leg.id)
        assert (o.status, o.synthetic, o.broker_link_id) == (OrderStatus.pending, True, None)
    assert len(_posted(fake)) == 1  # the buy only: nothing holds the coins at Alpaca


async def test_full_exit_is_sized_to_the_account_and_leaves_no_residue(
        maker, redis, qs, alpaca):
    """Booked more than the account holds (a fee estimated low): the close is sized down
    to what the account holds and takes the residue along (its value as fee)."""
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    residue = D("0.00000375")
    fake.alpaca.positions[PAIR] = NET - residue
    assert await _reconciled_ok(maker, link_id, http)  # a fee residue: no difference

    close = await _close(maker, redis, qs, pid)
    await _settle(sync)
    o = await _order(maker, close)
    assert (o.status, o.filled_qty) == (OrderStatus.filled, NET - residue)
    assert fake.alpaca.orders[o.broker_order_id]["qty"] == "0.0014925"
    [f] = await _fills(maker, close)
    assert f.qty == NET
    assert f.fee_amount == quantize_money(
        (NET - residue) * PRICE * D("0.0025") + residue * PRICE, "USD")
    assert fake.alpaca.positions == {}
    assert await _held(maker, pid) == (0, 0)
    assert await _reconciled_ok(maker, link_id, http)


async def test_full_exit_takes_the_account_residue_never_booked(maker, redis, qs, alpaca):
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    fake.alpaca.positions[PAIR] = NET + D("0.000001")  # a fee estimated high
    assert await _reconciled_ok(maker, link_id, http)

    close = await _close(maker, redis, qs, pid)
    await _settle(sync)
    o = await _order(maker, close)
    assert (o.status, o.filled_qty) == (OrderStatus.filled, NET + D("0.000001"))
    assert fake.alpaca.positions == {}
    assert await _held(maker, pid) == (0, 0)
    assert await _reconciled_ok(maker, link_id, http)


async def test_a_residue_the_account_no_longer_holds_is_closed_here(
        maker, redis, qs, alpaca):
    """Below the minimum order size and gone from the account: closed here, never sent
    (Alpaca would refuse it), its value as fee, no cash moved."""
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    oid = await _place(maker, redis, qs, pid, qty=D("0.000004"))
    await _settle(sync)
    [buy] = await _fills(maker, oid)
    fake.alpaca.positions.clear()  # e.g. the fee the broker took
    assert await _reconciled_ok(maker, link_id, http)
    posted = len(_posted(fake))

    close = await _close(maker, redis, qs, pid)
    await _settle(sync)
    o = await _order(maker, close)
    assert (o.status, o.filled_qty, o.broker_order_id) == (OrderStatus.filled, buy.qty, None)
    assert len(_posted(fake)) == posted
    [f] = await _fills(maker, close)
    assert (f.qty, f.price) == (buy.qty, PRICE)
    assert f.fee_amount == quantize_money(buy.qty * PRICE, "USD")
    assert await _held(maker, pid) == (0, 0)
    assert await _reconciled_ok(maker, link_id, http)


async def test_a_real_shortfall_is_not_hidden(maker, redis, qs, alpaca):
    """Beyond the fee residue the sell goes out as it is: the broker's refusal stands,
    and the reconciliation reports the difference."""
    fake, http = alpaca
    pid, link_id = await _portfolio(maker)
    sync = _sync(maker, redis, qs, http)
    await _buy(maker, redis, qs, pid, sync)
    fake.alpaca.positions[PAIR] = NET / 2
    assert not await _reconciled_ok(maker, link_id, http)
    close = await _close(maker, redis, qs, pid)
    await _settle(sync)
    o = await _order(maker, close)
    assert o.status == OrderStatus.rejected
    assert "insufficient balance" in (o.reject_message or "")
    assert await _held(maker, pid) == (NET, NET)


def test_compare_positions_takes_the_fee_residue_as_equal():
    items = compare_positions({SYM: NET, "AAPL": D(2)}, {SYM: NET - D("0.000004"),
                                                        "AAPL": D("1.99")}, [],
                              {SYM: D("0.00001709")})
    status = {i["symbol"]: i["status"] for i in items}
    assert status == {SYM: "ok", "AAPL": "mismatch"}
