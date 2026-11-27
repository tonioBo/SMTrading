"""Alpaca fees charged apart from the orders (found on 25/11: the REG and CAT fees of an
MSFT sale, 0,07 $, posted the night after as account activities FEE, booked 0 here).

The real AlpacaPaperAdapter on the FakeLiveBroker transport (FEE activities in the shape
the paper API answered on 25/11). `book_broker_fees` books every fee once, on the
portfolios whose broker fills of that trading day it is for, out of their cash.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.fee_activities import book_broker_fees, split_cents
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.sync import BrokerSync
from smtrading.common.db import make_engine
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, close_position, place_order
from smtrading.events import dispatch_post_commit
from smtrading.ledger.models import (
    Account,
    AccountBalance,
    AccountKind,
    Journal,
    JournalLine,
    JournalType,
)
from smtrading.performance.strategies import _fills as strategy_fills
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import JobRun
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

D = Decimal
SYM = "MSFT"
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)  # Monday 10:00 New York: NASDAQ open
DAY = "2026-11-23"
PRICE = D("503.00")
QTY = D("4.35")
CFG = {"key_id": "KID-CANARY-0001", "secret_key": "SEC-CANARY-0002"}


@pytest.fixture(autouse=True)
def _frozen_risk_clock(monkeypatch):
    """The quotes are stamped NOW: the risk check's wall clock must see NOW too."""
    import smtrading.risk.checks as risk_checks

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(risk_checks, "datetime", _Frozen)


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
    q.set_quote(SYM, D("502.90"), PRICE, D("502.95"), ts=NOW)
    return q


@pytest.fixture
async def alpaca():
    fake = FakeLiveBroker(price=PRICE)
    fake.alpaca.cash = D(100000)
    async with httpx.AsyncClient(transport=fake.transport()) as http:
        yield fake, http


async def _link(maker, kind="alpaca_paper", cfg=None) -> uuid.UUID:
    async with maker() as s:
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", kind, dict(cfg or CFG))
        await s.commit()
        return link.id


async def _portfolio(maker, link_id) -> uuid.UUID:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        await link_portfolio(s, p.id, link_id)
        await s.commit()
        return p.id


async def _trade(maker, redis, qs, sync, pid, *, close=False) -> uuid.UUID:
    async with maker() as s:
        if close:
            order = await close_position(s, qs, pid, SYM, now=NOW)
        else:
            order = await place_order(s, qs, PlaceOrderRequest(
                portfolio_id=pid, symbol=SYM, side=OrderSide.buy, type=OrderType.market,
                qty=QTY), now=NOW)
        await s.commit()
        await dispatch_post_commit(s, redis)
    for _ in range(3):
        await sync.tick()
    async with maker() as s:
        assert (await s.get(Order, order.id)).status == OrderStatus.filled
        await s.execute(update(Fill).where(Fill.order_id == order.id)
                        .values(created_at=NOW))  # the trading day of the fee
        await s.commit()
    return order.id


async def _usd_cash(maker, pid) -> Decimal:
    async with maker() as s:
        return await s.scalar(
            select(AccountBalance.balance)
            .join(Account, AccountBalance.account_id == Account.id)
            .where(Account.portfolio_id == pid, Account.asset == "USD",
                   Account.kind == AccountKind.cash)) or D(0)


async def _fee_journals(maker, pid) -> list[tuple[Journal, Decimal]]:
    async with maker() as s:
        return [(j, -q) for j, q in (await s.execute(
            select(Journal, JournalLine.quantity)
            .join(JournalLine, JournalLine.journal_id == Journal.id)
            .join(Account, Account.id == JournalLine.account_id)
            .where(Journal.type == JournalType.FEE, Account.portfolio_id == pid)
            .order_by(Journal.seq))).all()]


async def _warnings(maker) -> list[str]:
    async with maker() as s:
        return list(await s.scalars(select(TelegramOutbox.text).where(
            TelegramOutbox.category == "system", TelegramOutbox.level == "warning")))


async def _archive(maker, pid) -> None:
    async with maker() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == pid)
                        .values(status=PortfolioStatus.archived))
        await s.commit()


def _sync(maker, redis, qs, http) -> BrokerSync:
    return BrokerSync(maker, redis, qs, http, clock=lambda: NOW)


# -- the adapter ---------------------------------------------------------------------------

# the paper API's answer on 25/11 (the account id replaced)
PAPER_FEES = [
    {"id": "20261124000000000::b4b04a3c", "activity_type": "FEE",
     "activity_sub_type": "REG", "date": "2026-11-24",
     "created_at": "2026-11-25T01:15:47.59161Z", "net_amount": "-0.05",
     "description": "REG fee for proceed of $2133.54 on 2026-11-24 by PAXXXX",
     "status": "executed", "currency": "USD"},
    {"id": "20261124000000000::065aaad5", "activity_type": "FEE",
     "activity_sub_type": "CAT", "date": "2026-11-24",
     "created_at": "2026-11-25T01:05:31.905781Z", "net_amount": "-0.01",
     "description": "CAT fee for proceed of 2 trades on 2026-11-24 by PAXXXX",
     "status": "executed", "currency": "USD"},
    {"id": "20261124000000000::refund", "activity_type": "FEE", "date": "2026-11-24",
     "net_amount": "0.01", "description": "refund", "currency": "USD"},
]


async def test_adapter_lists_the_fee_activities_paged():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        assert request.method == "GET"
        assert request.url.path == "/v2/account/activities/FEE"
        ids = [a["id"] for a in PAPER_FEES]
        token = request.url.params.get("page_token")
        start = ids.index(token) + 1 if token else 0
        return httpx.Response(200, json=PAPER_FEES[start:start + 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = AlpacaPaperAdapter(dict(CFG), http)
        import smtrading.brokers.alpaca_core as core
        old, core._ACTIVITIES_PAGE = core._ACTIVITIES_PAGE, 1
        try:
            fees = await adapter.fee_activities(date(2026, 11, 20))
        finally:
            core._ACTIVITIES_PAGE = old
    assert [(f.kind, f.amount, f.trade_date, f.currency) for f in fees] == [
        ("REG", D("0.05"), date(2026, 11, 24), "USD"),
        ("CAT", D("0.01"), date(2026, 11, 24), "USD")]  # the refund is not a charge
    assert calls[0]["after"] == "2026-11-19" and calls[0]["direction"] == "asc"
    assert calls[1]["page_token"] == "20261124000000000::b4b04a3c"
    assert len(calls) == 4  # the last page is empty


def test_split_cents_adds_up_exactly():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert split_cents(D("0.03"), "USD", {a: D(180), b: D(90)}) == {a: D("0.02"),
                                                                   b: D("0.01")}
    shares = split_cents(D("0.10"), "USD", {a: D(1), b: D(1), c: D(1)})
    assert sum(shares.values()) == D("0.10") and sorted(shares.values()) == [
        D("0.03"), D("0.03"), D("0.04")]
    assert split_cents(D("0.01"), "USD", {a: D(1), b: D(3)}) == {b: D("0.01")}


# -- booking -------------------------------------------------------------------------------

async def _day(maker, redis, qs, http):
    """A link with a seller (bought and sold MSFT) and a buyer (bought only, so no USD
    cash: its EUR paid for the buy) on DAY."""
    link_id = await _link(maker)
    seller, buyer = await _portfolio(maker, link_id), await _portfolio(maker, link_id)
    sync = _sync(maker, redis, qs, http)
    await _trade(maker, redis, qs, sync, seller)
    sell = await _trade(maker, redis, qs, sync, seller, close=True)
    await _trade(maker, redis, qs, sync, buyer)
    return link_id, seller, buyer, sell


async def test_fees_are_booked_on_the_day_fills_once(maker, redis, qs, alpaca):
    fake, http = alpaca
    _, seller, buyer, sell = await _day(maker, redis, qs, http)
    before = await _usd_cash(maker, seller)
    assert await _usd_cash(maker, buyer) == 0

    fake.alpaca_charge_fee(DAY, D("0.05"), "REG", posted="2026-11-24")
    fake.alpaca_charge_fee(DAY, D("0.03"), "CAT")
    assert await book_broker_fees(maker, http, now=NOW) == 2
    assert await book_broker_fees(maker, http, now=NOW) == 0  # never twice

    # REG (on sells): the seller alone; CAT over every fill by notional (2:1)
    assert before - await _usd_cash(maker, seller) == D("0.07")
    seller_fees = await _fee_journals(maker, seller)
    assert [q for _, q in seller_fees] == [D("0.05"), D("0.02")]
    assert seller_fees[0][0].order_id == sell
    async with maker() as s:
        sell_fill = await s.scalar(select(Fill.id).where(Fill.order_id == sell))
    assert seller_fees[0][0].fill_id == sell_fill
    # the buyer holds no USD: never a margin loan, never another currency; told once
    assert await _fee_journals(maker, buyer) == []
    async with maker() as s:
        loans = await s.scalar(select(AccountBalance.balance)
                               .join(Account, AccountBalance.account_id == Account.id)
                               .where(Account.portfolio_id == buyer,
                                      Account.kind == AccountKind.margin_loan))
    assert not loans
    [warning] = await _warnings(maker)
    assert "0,01 USD" in warning and "insufficiente" in warning and "Da fare:" in warning

    # the «Strategie» costs count them, on the fills they are booked with
    async with maker() as s:
        fills, _, _ = await strategy_fills(s, portfolio_id=seller)
        trade_fees = sum(await s.scalars(select(Fill.fee_amount)
                                         .where(Fill.portfolio_id == seller,
                                                Fill.fee_currency == "USD")), D(0))
    assert sum(f.costs.get("USD", D(0)) for f in fills) - trade_fees == D("0.07")


async def test_a_share_of_an_archived_portfolio_is_not_moved(maker, redis, qs, alpaca):
    fake, http = alpaca
    _, seller, buyer, _ = await _day(maker, redis, qs, http)
    await _archive(maker, seller)
    fake.alpaca_charge_fee(DAY, D("0.05"), "REG")  # the archived seller's sale
    assert await book_broker_fees(maker, http, now=NOW) == 0
    assert await _fee_journals(maker, buyer) == []
    [warning] = await _warnings(maker)
    assert "archiviato" in warning
    assert await book_broker_fees(maker, http, now=NOW) == 0
    assert len(await _warnings(maker)) == 1  # claimed: told once


async def test_a_fee_with_no_fill_goes_to_the_only_portfolio_ever(
        maker, redis, qs, alpaca):
    fake, http = alpaca
    link_id = await _link(maker)
    pid = await _portfolio(maker, link_id)
    sync = _sync(maker, redis, qs, http)
    await _trade(maker, redis, qs, sync, pid)
    await _trade(maker, redis, qs, sync, pid, close=True)  # USD cash from the sale
    fake.alpaca_charge_fee("2026-11-20", D("0.01"), "CAT")  # no fill that day
    assert await book_broker_fees(maker, http, now=NOW) == 1
    [(j, q)] = await _fee_journals(maker, pid)
    assert q == D("0.01") and j.order_id is None
    assert await _warnings(maker) == []


async def test_a_fee_with_no_fill_and_several_portfolios_is_not_guessed(maker, alpaca):
    fake, http = alpaca
    link_id = await _link(maker)
    a, b = await _portfolio(maker, link_id), await _portfolio(maker, link_id)
    await _archive(maker, b)  # an archived one still counts
    fake.alpaca_charge_fee("2026-11-20", D("0.01"), "CAT")
    assert await book_broker_fees(maker, http, now=NOW) == 0
    assert await _fee_journals(maker, a) == [] and await _fee_journals(maker, b) == []
    [warning] = await _warnings(maker)
    assert "più portafogli" in warning


async def test_unhandled_fees_and_zero_amounts_are_claimed_not_booked(maker, alpaca):
    fake, http = alpaca
    link_id = await _link(maker)
    pid = await _portfolio(maker, link_id)
    fake.alpaca_charge_fee("2026-11-20", D("0.01"), "ACAT")
    fake.alpaca_charge_fee("2026-11-20", D("0.004"), "CAT")
    assert await book_broker_fees(maker, http, now=NOW) == 0
    assert await _fee_journals(maker, pid) == []
    [warning] = await _warnings(maker)
    assert "tipo non gestito (ACAT" in warning
    async with maker() as s:
        claims = await s.scalar(select(func.count()).select_from(JobRun)
                                .where(JobRun.job == "broker_fee"))
    assert claims == 2


async def test_other_brokers_are_not_called(maker, alpaca):
    fake, http = alpaca
    await _link(maker, "etoro_demo", {"api_key": "K-CANARY", "user_key": "U-CANARY"})
    assert await book_broker_fees(maker, http, now=NOW) == 0
    assert not [p for _, p in fake.calls if "activities" in p or p.startswith("/api/")]


async def test_old_fees_are_out_of_the_lookback(maker, alpaca):
    fake, http = alpaca
    link_id = await _link(maker)
    await _portfolio(maker, link_id)
    fake.alpaca_charge_fee("2026-11-01", D("0.01"), "CAT")
    assert await book_broker_fees(maker, http, now=NOW) == 0
