"""The end-of-session safety close (`intraday.day_close.DayCloser`): `day_close_minutes`
before the session end, the day quantity of every portfolio with mode A or mode B on is
sold reduce-only by the system (at market, or with a DAY limit on a wide spread), once
per session; a Telegram notice for each close, one more if the day quantity is still open
a minute before the end. Never with the market closed. NYSE: Tuesday 1/12/2026 closes at
21:00Z; Thursday 26/11 is Thanksgiving; Friday 27/11 closes early at 18:00Z."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.db import make_engine
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.processor import OrderProcessor
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, close_position, place_order
from smtrading.events import discard_post_commit
from smtrading.instruments.models import Instrument
from smtrading.intraday.day_close import DayCloser
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

BOUGHT = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)
YESTERDAY = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 12, 1, 20, 50, 30, tzinfo=UTC)
CLOSE = datetime(2026, 12, 1, 21, 0, tzinfo=UTC)


@pytest.fixture
async def sm(fresh_db, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await run_seed(s)
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.commit()
    yield maker
    await engine.dispose()


class QS(FakeQuoteSource):
    """Quotes stamped at `at` (the test's clock)."""

    def __init__(self):
        super().__init__()
        self.at: datetime | None = None

    async def get_quotes(self, symbols, *, max_age_s: int = 5):
        quotes = await super().get_quotes(symbols, max_age_s=max_age_s)
        if self.at is None:
            return quotes
        return {k: dataclasses.replace(q, ts=self.at) for k, q in quotes.items()}


@pytest.fixture
def qs():
    q = QS()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    return q


async def _portfolio(sm, *, intraday=True, auto=False) -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=f"Giorno {uuid.uuid4().hex[:6]}",
                                   initial_cash=D(100000), leverage=D(1))
        p.intraday_enabled, p.auto_rules_enabled = intraday, auto
        discard_post_commit(s)
        await s.commit()
        return p.id


CLOCK: dict[str, datetime | None] = {"now": None}


@pytest.fixture(autouse=True)
def _risk_clock(monkeypatch):
    """The risk check reads the wall clock for a quote's age; the quotes here are stamped
    at the test's clock (`QS.at`), so the check sees that clock too: the tests never
    depend on the real date."""
    import smtrading.risk.checks as risk_checks

    class _Clocked(datetime):
        @classmethod
        def now(cls, tz=None):
            now = CLOCK["now"] or datetime.now(UTC)
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(risk_checks, "datetime", _Clocked)
    CLOCK["now"] = None
    yield
    CLOCK["now"] = None


async def _buy(sm, qs, pid, qty, at, *, day_trade=True):
    """A filled buy whose fill is dated `at` (fills are stamped by the database clock)."""
    qs.at = CLOCK["now"] = at
    async with sm() as s:
        o = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(qty), day_trade=day_trade), now=at)
        assert o.status == OrderStatus.filled
        await s.execute(update(Fill).where(Fill.order_id == o.id).values(created_at=at))
        discard_post_commit(s)
        await s.commit()
    qs.at = None


async def _tick(sm, qs, now):
    qs.at = CLOCK["now"] = now
    try:
        await DayCloser(sm, qs, None).tick(now)
    finally:
        qs.at = None


async def _system_orders(sm, pid=None) -> list[Order]:
    async with sm() as s:
        q = select(Order).where(Order.source == "system").order_by(Order.created_at)
        if pid is not None:
            q = q.where(Order.portfolio_id == pid)
        return list(await s.scalars(q))


async def _outbox(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def test_closes_day_qty_at_cutoff(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    orders = await _system_orders(sm)
    assert len(orders) == 1
    o = orders[0]
    assert (o.portfolio_id, o.side, o.type, o.qty, o.reduce_only, o.agent_id) == (
        pid, OrderSide.sell, OrderType.market, D(10), True, None)
    assert o.client_order_id == "smt-dayclose-20261201-AAPL"
    assert o.status == OrderStatus.filled
    rows = await _outbox(sm)
    assert len(rows) == 1
    assert (rows[0].source, rows[0].category) == ("system", "system")
    assert rows[0].text.startswith("Chiusura di sicurezza · Giorno ")
    assert "AAPL: 10 in vendita a mercato, 10 minuti prima della fine seduta." in rows[0].text
    assert "Motivo: posizione di giornata ancora aperta." in rows[0].text
    assert "Restano" not in rows[0].text


async def test_auto_rules_portfolio_is_covered(sm, qs):
    pid = await _portfolio(sm, intraday=False, auto=True)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    orders = await _system_orders(sm)
    assert len(orders) == 1 and orders[0].portfolio_id == pid and orders[0].qty == D(10)


async def test_safety_limit_is_day_order(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    qs.set_quote("AAPL", D(95), D(105), D(100))
    await _tick(sm, qs, CUTOFF)
    [o] = await _system_orders(sm)
    assert o.type == OrderType.limit and o.limit_price == D("99.50")
    assert o.tif == TimeInForce.day
    assert o.expires_at == CLOSE
    assert o.status == OrderStatus.pending
    after = CLOSE + timedelta(seconds=30)
    await OrderProcessor(sm, None, qs, clock=lambda: after)._expire(after)
    async with sm() as s:
        assert (await s.get(Order, o.id)).status == OrderStatus.expired


async def test_wide_spread_limit_order(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    async with sm() as s:  # the limit is floored to the instrument's tick
        await s.execute(update(Instrument).where(Instrument.symbol == "AAPL")
                        .values(tick_size=D("0.05")))
        await s.commit()
    qs.set_quote("AAPL", D(95), D(105), D("100.013"))
    await _tick(sm, qs, CUTOFF)
    [o] = await _system_orders(sm)
    assert (o.type, o.limit_price, o.qty, o.reduce_only) == (
        OrderType.limit, D("99.50"), D(10), True)
    [row] = await _outbox(sm)
    assert "AAPL: 10 in vendita con limite 99,50 (forbice larga)" in row.text


async def test_wide_spread_limit_with_the_seeded_tick_is_in_cents(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    qs.set_quote("AAPL", D(95), D(105), D("100.013"))  # seeded tick 0.0001: no sub-penny
    await _tick(sm, qs, CUTOFF)
    [o] = await _system_orders(sm)
    assert (o.type, o.limit_price) == (OrderType.limit, D("99.51"))
    [row] = await _outbox(sm)
    assert "con limite 99,51 (forbice larga)" in row.text


async def test_wide_spread_without_last_sends_nothing(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    qs.set_quote("AAPL", D(95), D(105), None)
    await _tick(sm, qs, CUTOFF)
    assert await _system_orders(sm, pid) == []
    [row] = await _outbox(sm)
    assert "chiusura di sicurezza impossibile" in row.text.lower()
    assert "nessun prezzo affidabile" in row.text


async def test_not_before_cutoff(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, datetime(2026, 12, 1, 20, 49, tzinfo=UTC))
    assert await _system_orders(sm) == []
    assert await _outbox(sm) == []


async def test_never_with_market_closed(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, datetime(2026, 11, 26, 20, 50, 30, tzinfo=UTC))  # Thanksgiving
    await _tick(sm, qs, CLOSE + timedelta(seconds=30))
    assert await _system_orders(sm) == []
    assert await _outbox(sm) == []


async def test_half_day_cutoff(sm, qs):
    pid = await _portfolio(sm)
    friday = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    await _buy(sm, qs, pid, 10, friday)
    await _tick(sm, qs, datetime(2026, 11, 27, 20, 50, tzinfo=UTC))  # after the early close
    assert await _system_orders(sm) == []
    await _tick(sm, qs, datetime(2026, 11, 27, 17, 50, tzinfo=UTC))
    [o] = await _system_orders(sm)
    assert o.client_order_id == "smt-dayclose-20261127-AAPL" and o.qty == D(10)


async def test_mixed_position_sells_only_day_qty(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 60, YESTERDAY, day_trade=False)
    await _buy(sm, qs, pid, 40, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    [o] = await _system_orders(sm)
    assert o.qty == D(40)
    [row] = await _outbox(sm)
    assert "AAPL: 40 in vendita a mercato" in row.text
    assert ("Restano 60 comprati prima di oggi: riproteggili (stop e obiettivo del simbolo "
            "annullati).") in row.text


async def test_unfilled_alert_at_one_minute(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    qs.set_quote("AAPL", D(95), D(105), D(100))
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, datetime(2026, 12, 1, 20, 59, 10, tzinfo=UTC))
    await _tick(sm, qs, datetime(2026, 12, 1, 20, 59, 40, tzinfo=UTC))
    assert len(await _system_orders(sm)) == 1
    texts = [r.text for r in await _outbox(sm)]
    assert len(texts) == 2
    assert texts[1].startswith("Chiusura di sicurezza non eseguita · Giorno ")
    assert texts[1].endswith("AAPL: 10 ancora aperti a 1 minuto dalla fine")


async def test_no_unfilled_alert_after_a_filled_close(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, datetime(2026, 12, 1, 20, 59, 10, tzinfo=UTC))
    assert len(await _outbox(sm)) == 1


async def test_run_once_per_session(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    qs.set_quote("AAPL", D(95), D(105), D(100))  # left resting: the day quantity stays
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, CUTOFF + timedelta(minutes=2))
    assert len(await _system_orders(sm)) == 1
    assert len(await _outbox(sm)) == 1


async def test_refused_close_is_reported(sm, qs, monkeypatch):
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.intraday import day_close

    calls = []

    async def refuse(*args, **kwargs):
        calls.append(1)
        raise SMTError(ErrorCode.INVALID_ORDER, "order not valid")

    monkeypatch.setattr(day_close, "close_position", refuse)
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, CUTOFF + timedelta(minutes=1))
    [row] = await _outbox(sm)
    assert "INVALID_ORDER" in row.text and "order not valid" in row.text
    assert len(calls) == 1  # a permanent refusal is not retried


@pytest.mark.parametrize("code", ["STALE_QUOTE", "PROVIDER_UNAVAILABLE", "PORTFOLIO_LOCKED",
                                  "RATE_LIMITED", "TRADING_HALTED"])
async def test_temporary_refusal_is_retried_next_tick(sm, qs, monkeypatch, code):
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.intraday import day_close

    real = day_close.close_position
    calls = []

    async def refuse_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise SMTError(ErrorCode(code), "temporarily refused")
        return await real(*args, **kwargs)

    monkeypatch.setattr(day_close, "close_position", refuse_once)
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    assert await _system_orders(sm, pid) == [] and await _outbox(sm) == []
    await _tick(sm, qs, CUTOFF + timedelta(seconds=15))
    [o] = await _system_orders(sm, pid)
    assert (o.side, o.qty, o.status) == (OrderSide.sell, D(10), OrderStatus.filled)
    [row] = await _outbox(sm)
    assert "10 in vendita a mercato" in row.text
    assert len(calls) == 2


async def test_temporary_refusal_in_the_last_minute_is_reported(sm, qs, monkeypatch):
    from smtrading.common.errors import ErrorCode, SMTError
    from smtrading.intraday import day_close

    calls = []

    async def refuse(*args, **kwargs):
        calls.append(1)
        raise SMTError(ErrorCode.STALE_QUOTE, "quote too old")

    monkeypatch.setattr(day_close, "close_position", refuse)
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, CUTOFF + timedelta(minutes=3))
    assert await _outbox(sm) == []  # retried silently while there is time
    last = CLOSE - timedelta(seconds=50)  # in the last minute: reported, not retried
    await _tick(sm, qs, last)
    await _tick(sm, qs, last + timedelta(seconds=15))
    texts = [r.text for r in await _outbox(sm)]
    assert sum("Chiusura di sicurezza rifiutata" in t and "STALE_QUOTE" in t
               for t in texts) == 1
    assert len(calls) == 3


async def test_off_portfolio_untouched(sm, qs):
    pid = await _portfolio(sm, intraday=False, auto=False)
    await _buy(sm, qs, pid, 10, BOUGHT)
    await _tick(sm, qs, CUTOFF)
    await _tick(sm, qs, datetime(2026, 12, 1, 20, 59, 10, tzinfo=UTC))
    assert await _system_orders(sm) == []
    assert await _outbox(sm) == []


async def test_close_position_defaults_unchanged(sm, qs):
    pid = await _portfolio(sm, intraday=False)
    await _buy(sm, qs, pid, 10, BOUGHT, day_trade=False)
    qs.at = CLOCK["now"] = BOUGHT
    async with sm() as s:
        o = await close_position(s, qs, pid, "AAPL", source="system", now=BOUGHT)
        discard_post_commit(s)
        await s.commit()
    assert o.type == OrderType.market and o.tif == TimeInForce.gtc
    assert o.client_order_id is None and o.limit_price is None and o.reduce_only is True
    assert o.status == OrderStatus.filled
