"""End-to-end engine scenario on a committed database (fresh_db).

Demo (100000 EUR) buys BTC-USD with FX, sells it with a limit order filled by a tick,
runs a bracket on AAPL closed by its take profit, shorts and covers ENI.MI and is charged
the daily interest. The ledger must stay consistent and the realized P&L of the position
projection must match an independent computation from the fills.
"""

import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading import cli
from smtrading.common.db import make_engine
from smtrading.corporate.interest import run_interest
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    place_bracket_order,
    place_order,
)
from smtrading.events import dispatch_post_commit
from smtrading.ledger.models import Account, Journal, JournalLine, JournalType
from smtrading.ledger.service import verify_balances, verify_chain
from smtrading.portfolios.models import Portfolio
from smtrading.portfolios.service import get_cash_balances
from smtrading.positions.models import Position
from smtrading.seed import DEMO_PORTFOLIO_NAME, run_seed
from smtrading.valuation.service import value_portfolio

D = Decimal
NOW = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)  # Wednesday: NASDAQ, MTA and CRYPTO open


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


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
    q.set_fx("EUR", "USD", D("1.10"))
    q.set_quote("BTC-USD", D("50000.00"), D("50010.00"), D("50005.00"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("ENI.MI", D("14.990"), D("15.000"), D("14.995"))
    return q


async def _run(maker, redis, action):
    """Run `action(session)` in its own transaction, commit, then dispatch effects."""
    async with maker() as s:
        result = await action(s)
        await s.commit()
        await dispatch_post_commit(s, redis)
        return result


def _req(pid, symbol, side, qty, type=OrderType.market, **kw) -> PlaceOrderRequest:
    return PlaceOrderRequest(
        portfolio_id=pid, symbol=symbol, side=side, type=type, qty=D(qty), **kw
    )


def _tick(qs, symbol, bid, ask, ts):
    qs.set_quote(symbol, D(bid), D(ask), None, ts=ts)
    return qs._quotes[symbol]


async def _order(maker, order_id) -> Order:
    async with maker() as s:
        return await s.get(Order, order_id)


async def test_engine_end_to_end(maker, redis, qs):
    # 1. Demo with 100000 EUR
    async with maker() as s:
        pid = await s.scalar(select(Portfolio.id).where(Portfolio.name == DEMO_PORTFOLIO_NAME))
        assert await get_cash_balances(s, pid) == {"EUR": D("100000.00")}

    # 2. market buy 0.5 BTC-USD, paid in EUR through an FX conversion
    btc_buy = await _run(maker, redis, lambda s: place_order(
        s, qs, _req(pid, "BTC-USD", OrderSide.buy, "0.5"), now=NOW))
    assert btc_buy.status == OrderStatus.filled and btc_buy.filled_qty == D("0.5")
    async with maker() as s:
        fx = await s.scalar(select(func.count()).select_from(Journal).where(
            Journal.type == JournalType.FX, Journal.order_id == btc_buy.id))
        assert fx == 1

    # 3. limit sell above the market rests, then a tick fills it
    btc_sell = await _run(maker, redis, lambda s: place_order(
        s, qs, _req(pid, "BTC-USD", OrderSide.sell, "0.5", OrderType.limit,
                    limit_price=D("51000.00")), now=NOW))
    assert btc_sell.status == OrderStatus.pending
    clock = Clock(NOW)
    proc = OrderProcessor(maker, redis, qs, clock=clock)
    assert await proc.on_tick("BTC-USD", _tick(qs, "BTC-USD", "50900.00", "50910.00", NOW)) == 0
    clock.advance(1)
    tick = _tick(qs, "BTC-USD", "51050.00", "51060.00", clock.now)
    assert await proc.on_tick("BTC-USD", tick) == 1
    btc_sell = await _order(maker, btc_sell.id)
    assert btc_sell.status == OrderStatus.filled
    assert btc_sell.avg_fill_price >= D("51000.00")

    # 4. bracket on AAPL: entry fills at once, the take profit fills on a tick and
    #    cancels the stop loss
    entry, tp, sl = await _run(maker, redis, lambda s: place_bracket_order(
        s, qs, _req(pid, "AAPL", OrderSide.buy, "10"),
        take_profit=D("210.00"), stop_loss=D("190.00"), now=NOW))
    assert entry.status == OrderStatus.filled
    assert (tp.status, sl.status) == (OrderStatus.pending, OrderStatus.pending)
    clock.advance(1)
    assert await proc.on_tick("AAPL", _tick(qs, "AAPL", "210.50", "210.60", clock.now)) == 1
    assert (await _order(maker, tp.id)).status == OrderStatus.filled
    assert (await _order(maker, sl.id)).status == OrderStatus.canceled

    # 5. short 200 ENI.MI, cover 100, and (after the interest job) the remaining 100
    short = await _run(maker, redis, lambda s: place_order(
        s, qs, _req(pid, "ENI.MI", OrderSide.sell, "200"), now=NOW))
    assert short.status == OrderStatus.filled
    qs.set_quote("ENI.MI", D("14.490"), D("14.510"), D("14.500"))
    cover = await _run(maker, redis, lambda s: place_order(
        s, qs, _req(pid, "ENI.MI", OrderSide.buy, "100"), now=NOW))
    assert cover.status == OrderStatus.filled

    # 6. interest job: the borrow fee of the remaining short, |100 x 14.50| x 3% / 360
    async with maker() as s:
        assert await run_interest(s, qs, date(2026, 11, 11)) == 1
        fee = await s.scalar(
            select(JournalLine.quantity).join(Journal, Journal.id == JournalLine.journal_id)
            .join(Account, Account.id == JournalLine.account_id)
            .where(Journal.type == JournalType.INTEREST, Account.portfolio_id == pid)
        )
        assert fee == D("-0.12")
    final = await _run(maker, redis, lambda s: close_position(
        s, qs, pid, "ENI.MI", now=NOW))
    assert final.status == OrderStatus.filled

    # final checks
    async with maker() as s:
        assert (await verify_chain(s)).ok is True
        assert await verify_balances(s) == []
        unbalanced = (await s.execute(
            select(JournalLine.journal_id, Account.asset)
            .join(Account, Account.id == JournalLine.account_id)
            .group_by(JournalLine.journal_id, Account.asset)
            .having(func.sum(JournalLine.quantity) != 0)
        )).all()
        assert unbalanced == []

        fills = list(await s.scalars(select(Fill).where(Fill.portfolio_id == pid)))
        assert len(fills) == 7
        expected: dict[str, Decimal] = defaultdict(Decimal)
        with localcontext() as ctx:
            ctx.prec = 60
            for f in fills:  # every symbol is flat: realized = sells - buys
                value = f.qty * f.price
                expected[f.symbol] += value if f.side == OrderSide.sell else value.copy_negate()
        positions = list(await s.scalars(select(Position).where(Position.portfolio_id == pid)))
        assert all(p.qty == 0 for p in positions)
        realized = {p.symbol: p.realized_pnl for p in positions}
        assert realized == dict(expected)
        assert set(realized) == {"BTC-USD", "AAPL", "ENI.MI"}
        assert realized["BTC-USD"] > 0 and realized["AAPL"] > 0 and realized["ENI.MI"] > 0

        valuation = await value_portfolio(s, qs, pid)
        assert valuation.positions == [] and valuation.exposure_raw == 0
        assert valuation.margin_level() == "ok"


# -- CLI (python -m smtrading.cli) ------------------------------------------------------


async def _cli(maker, qs, redis, *argv):
    code, out = await cli.run_command(cli.build_parser().parse_args(argv), maker, qs, redis)
    return code, json.loads(out)


async def test_cli_order_positions_valuation_cancel(maker, redis, qs):
    code, order = await _cli(maker, qs, redis, "order", "place", "--portfolio", "Demo",
                             "--symbol", "BTC-USD", "--side", "buy", "--qty", "0.01")
    assert code == 0
    assert order["status"] == "filled" and order["filled_qty"] == "0.01"
    assert order["source"] == "admin"

    code, positions = await _cli(maker, qs, redis, "positions", "--portfolio", "Demo")
    assert code == 0
    assert [(p["symbol"], p["qty"]) for p in positions] == [("BTC-USD", "0.01")]

    code, valuation = await _cli(maker, qs, redis, "valuation", "--portfolio", "Demo")
    assert code == 0
    assert D("99900") < D(valuation["equity_eur"]) < D("100000")
    assert [p["symbol"] for p in valuation["positions"]] == ["BTC-USD"]

    code, limit = await _cli(maker, qs, redis, "order", "place", "--portfolio", "Demo",
                             "--symbol", "BTC-USD", "--side", "buy", "--qty", "0.01",
                             "--type", "limit", "--limit-price", "1000")
    assert code == 0 and limit["status"] == "pending"
    code, orders = await _cli(maker, qs, redis, "orders", "--portfolio", "Demo")
    assert code == 0
    assert [o["id"] for o in orders] == [limit["id"], order["id"]]

    code, canceled = await _cli(maker, qs, redis, "cancel", limit["id"])
    assert code == 0 and canceled["status"] == "canceled"
    assert (await _order(maker, uuid.UUID(limit["id"]))).status == OrderStatus.canceled


async def test_cli_rejection_is_stored_and_reported(maker, redis, qs):
    code, out = await _cli(maker, qs, redis, "order", "place", "--portfolio", "Demo",
                           "--symbol", "BTC-USD", "--side", "sell", "--qty", "1")
    assert code == 1
    assert out["error"]["code"] == "SHORT_NOT_ALLOWED"
    order = await _order(maker, uuid.UUID(out["error"]["details"]["order_id"]))
    assert order.status == OrderStatus.rejected


async def test_cli_errors(maker, redis, qs):
    code, out = await _cli(maker, qs, redis, "positions", "--portfolio", "Nope")
    assert code == 1 and out["error"]["code"] == "NOT_FOUND"
    code, out = await _cli(maker, qs, redis, "cancel", str(uuid.uuid4()))
    assert code == 1 and out["error"]["code"] == "NOT_FOUND"


def test_cli_rejects_a_malformed_quantity(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["order", "place", "--portfolio", "Demo", "--symbol", "BTC-USD",
             "--side", "buy", "--qty", "abc"]
        )
    assert "invalid decimal" in capsys.readouterr().err
