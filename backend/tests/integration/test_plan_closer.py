"""Chiusura dei piani (plan 17): the worker job closes the positions of an ended piano not
in loss at the first open session, puts the losing ones in grace until the deadline and
closes them then; a closed market waits, a stale quote is skipped, the kill switch lets
the closes through, a restart does nothing again; the risk check refuses to increase a
position in grace. The week of 11-17 January 2027 ends on Sunday; Monday 18 is a US
holiday (Martin Luther King Day), so AAPL's first session is Tuesday 19."""

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.activity.models import Activity
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    modify_order,
    place_oco_order,
    place_order,
)
from smtrading.events import discard_post_commit
from smtrading.goals.models import Goal, PlanClosure
from smtrading.goals.plan_closer import FAIL_NOTICE_AT, PlanCloser, plan_states
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.positions.models import Position
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed
from smtrading.telegram.models import TelegramOutbox

ROME = ZoneInfo("Europe/Rome")
BOUGHT = datetime(2027, 1, 15, 15, 0, tzinfo=UTC)  # Friday, NASDAQ open
SUNDAY_NIGHT = datetime(2027, 1, 17, 23, 5, tzinfo=UTC)  # Monday 00:05 in Rome
HOLIDAY = datetime(2027, 1, 18, 15, 0, tzinfo=UTC)  # Monday: US markets closed
TUESDAY = datetime(2027, 1, 19, 15, 0, tzinfo=UTC)  # NASDAQ open; closes at 21:00 UTC
DEADLINE = datetime(2027, 1, 19, 21, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
async def sm(fresh_db, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    engine = make_engine(fresh_db)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await run_seed(s)
        await s.execute(update(Portfolio).values(status=PortfolioStatus.archived))
        await s.execute(update(Goal).values(active=False))
        await s.commit()
    yield maker
    await engine.dispose()


class QS(FakeQuoteSource):
    """Quotes stamped at the job's clock (`at`), `age` before it."""

    def __init__(self):
        super().__init__()
        self.at: datetime | None = None
        self.age = timedelta(0)

    async def get_quotes(self, symbols, *, max_age_s: int = 5):
        quotes = await super().get_quotes(symbols, max_age_s=max_age_s)
        if self.at is None:
            return quotes
        return {k: dataclasses.replace(q, ts=self.at - self.age) for k, q in quotes.items()}


@pytest.fixture
def qs():
    q = QS()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_quote("AAPL", D("199.90"), D("200.00"), D("199.95"))
    q.set_quote("BTC-USD", D(50000), D(50010), D(50005))
    return q


async def _portfolio(sm, name="Piano P") -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(100000), leverage=D(1))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _buy(sm, qs, pid, symbol, qty, *, now=BOUGHT, side=OrderSide.buy):
    async with sm() as s:
        await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol=symbol, side=side, type=OrderType.market, qty=D(qty)),
            now=now)
        discard_post_commit(s)
        await s.commit()


async def _plan(sm, **kw) -> uuid.UUID:
    values = {"name": "settimana", "horizon": "week", "recurring": True, "scope": "total",
              "kind": "min", "target_pct": D(2), "active": True, "closes_positions": True,
              "plan_since": datetime(2027, 1, 1, tzinfo=UTC),
              "start_date": date(2027, 1, 4), **kw}
    async with sm() as s:
        g = Goal(**values)
        s.add(g)
        await s.commit()
        return g.id


async def _rows(sm) -> dict[str, PlanClosure]:
    async with sm() as s:
        return {r.symbol: r for r in await s.scalars(select(PlanClosure))}


async def _outbox(sm) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox.text).order_by(TelegramOutbox.id)))


async def _feed(sm, prefix="plan.") -> list[tuple[str, str]]:
    async with sm() as s:
        rows = await s.scalars(select(Activity).where(Activity.type.like(f"{prefix}%"))
                               .order_by(Activity.id))
        return [(a.type, a.summary) for a in rows]


async def _qty(sm, pid, symbol) -> D:
    async with sm() as s:
        p = await s.get(Position, (pid, symbol))
        return p.qty if p else D(0)


class _Job(PlanCloser):
    async def tick(self):
        self._qs.at = self._clock()
        try:
            await super().tick()
        finally:
            self._qs.at = None


def _job(sm, qs, clock):
    return _Job(sm, qs, None, clock=clock, tz="Europe/Rome")


async def test_end_grace_and_deadline(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    await _plan(sm)
    clock = Clock(SUNDAY_NIGHT)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))  # in loss
    qs.set_quote("BTC-USD", D(51000), D(51010), D(51005))  # in profit
    await _job(sm, qs, clock).tick()
    rows = await _rows(sm)
    assert {s: r.state for s, r in rows.items()} == {"AAPL": "pending", "BTC-USD": "closed"}, \
        {s: r.last_error for s, r in rows.items()}
    assert rows["BTC-USD"].closed_by == "system"
    assert await _qty(sm, pid, "BTC-USD") == 0
    out = await _outbox(sm)
    assert out[0].startswith("🏁 Piano «settimana» concluso (11/01–17/01): 2 posizioni")
    assert "🔒 Piano «settimana»: BTC-USD chiusa da SMTrading" in out[1]
    feed = await _feed(sm)
    assert [t for t, _ in feed] == ["plan.ended", "plan.closed"]
    assert "simul" not in " ".join(t for _, t in feed).lower()

    clock.t = HOLIDAY  # NASDAQ closed: waits
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"

    clock.t = TUESDAY
    await _job(sm, qs, clock).tick()
    aapl = (await _rows(sm))["AAPL"]
    assert (aapl.state, aapl.deadline) == ("grace", DEADLINE)
    assert [t for t, _ in await _feed(sm)][-1] == "plan.grace"

    # no additions while in grace; reducing is allowed
    with pytest.raises(SMTError) as e:
        await _buy(sm, qs, pid, "AAPL", 1, now=TUESDAY)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert e.value.details["reason"] == "plan_grace"
    assert e.value.details["symbol"] == "AAPL"
    assert e.value.details["deadline"] == DEADLINE.isoformat()
    await _buy(sm, qs, pid, "AAPL", 2, now=TUESDAY, side=OrderSide.sell)

    async with sm() as s:
        [state] = await plan_states(s)
    assert state["counts"] == {"pending": 0, "grace": 1, "closing": 0, "closed": 1}

    clock.t = DEADLINE - timedelta(minutes=30)  # before the last quarter of an hour
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "grace"
    clock.t = DEADLINE - timedelta(minutes=10)
    await _job(sm, qs, clock).tick()
    aapl = (await _rows(sm))["AAPL"]
    assert (aapl.state, aapl.closed_by) == ("closed", "system")
    assert await _qty(sm, pid, "AAPL") == 0
    out = await _outbox(sm)
    assert "fine del periodo di grazia" in out[-1]

    # a restart sends nothing again and creates nothing
    before = (len(await _outbox(sm)), len(await _feed(sm)))
    await _job(sm, qs, clock).tick()
    assert (len(await _outbox(sm)), len(await _feed(sm))) == before
    async with sm() as s:
        orders = list(await s.scalars(select(Order).where(Order.source == "system")))
    assert len(orders) == 2


async def test_stale_quote_skips_then_closes_and_kill_switch_lets_it_through(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    await _plan(sm, scope="portfolio", portfolio_id=pid)
    async with sm() as s:
        await set_kill_switch(s, True)
        await s.commit()
    clock = Clock(SUNDAY_NIGHT)
    qs.set_quote("BTC-USD", D(51000), D(51010), D(51005))
    qs.age = timedelta(hours=2)
    await _job(sm, qs, clock).tick()
    row = (await _rows(sm))["BTC-USD"]
    assert row.state == "pending" and row.last_error
    qs.age = timedelta(0)
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["BTC-USD"].state == "closed"
    assert await _qty(sm, pid, "BTC-USD") == 0


async def test_refused_close_is_retried_and_the_owner_told_once(sm, qs, monkeypatch):
    import smtrading.goals.plan_closer as mod

    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    await _plan(sm)
    qs.set_quote("BTC-USD", D(51000), D(51010), D(51005))

    async def refuse(*args, **kwargs):
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "quote unavailable")

    monkeypatch.setattr(mod, "close_position", refuse)
    clock = Clock(SUNDAY_NIGHT)
    for _ in range(FAIL_NOTICE_AT + 1):
        await _job(sm, qs, clock).tick()
    row = (await _rows(sm))["BTC-USD"]
    assert (row.state, row.attempts) == ("pending", FAIL_NOTICE_AT + 1)
    assert sum("non riuscita" in t for t in await _outbox(sm)) == 1
    monkeypatch.setattr(mod, "close_position", close_position)
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["BTC-USD"].state == "closed"


async def test_positions_closed_by_agents_or_opened_after_the_end(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _plan(sm)
    clock = Clock(SUNDAY_NIGHT)
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"
    async with sm() as s:  # the agents close it on Tuesday
        await close_position(s, qs, pid, "AAPL", now=TUESDAY)
        discard_post_commit(s)
        await s.commit()
    clock.t = TUESDAY
    await _job(sm, qs, clock).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.closed_by) == ("closed", "agent")
    assert not any(t.startswith("🔒") for t in await _outbox(sm))
    # bought again during the new period: not the piano's (its end is past)
    await _buy(sm, qs, pid, "AAPL", 5, now=TUESDAY)
    await _job(sm, qs, clock).tick()
    assert await _qty(sm, pid, "AAPL") == 5


async def test_deactivated_plan_and_plan_since(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    gid = await _plan(sm)
    late = await _plan(sm, name="tardi", scope="portfolio", portfolio_id=pid,
                       plan_since=datetime(2027, 1, 18, tzinfo=UTC))
    clock = Clock(SUNDAY_NIGHT)
    await _job(sm, qs, clock).tick()
    async with sm() as s:  # the later piano became one after the end: nothing
        assert await s.scalar(select(PlanClosure).where(PlanClosure.goal_id == late)) is None
    clock.t = TUESDAY
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "grace"
    async with sm() as s:
        await s.execute(update(Goal).where(Goal.id == gid).values(active=False))
        await s.commit()
    await _buy(sm, qs, pid, "AAPL", 1, now=TUESDAY)  # no longer a piano: allowed
    clock.t = DEADLINE - timedelta(minutes=5)
    await _job(sm, qs, clock).tick()
    assert await _qty(sm, pid, "AAPL") == 11


async def test_piano_since_mid_week_handles_the_current_week(sm, qs):
    """A goal that becomes a piano on Wednesday (the migrated weekly goal) handles that
    same week's end, positions opened before the flag included."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _plan(sm, plan_since=datetime(2027, 1, 13, 10, 0, tzinfo=UTC))
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"


async def test_no_second_close_while_the_agents_close_is_in_flight(sm, qs):
    """The agents' CHIUDI queued on the holiday is still open at Tuesday's open: the job
    places no second full-size close (at the broker it could sell twice)."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _plan(sm)
    qs.set_quote("AAPL", D("210.00"), D("210.10"), D("210.05"))  # in profit
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    async with sm() as s:  # market closed: the close waits in the queue
        agent_close = await close_position(s, qs, pid, "AAPL", now=HOLIDAY)
        discard_post_commit(s)
        await s.commit()
    assert agent_close.status == OrderStatus.queued
    await _job(sm, qs, Clock(TUESDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.last_error) == ("pending", "another close order is in flight")
    async with sm() as s:
        assert await s.scalar(select(Order).where(Order.source == "system")) is None


async def test_the_close_cancels_the_standalone_oco(sm, qs):
    """A PROTEGGI OCO (not reduce-only) is canceled with the plan's close: flat, its stop
    would open a short."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    async with sm() as s:
        legs = await place_oco_order(
            s, qs,
            PlaceOrderRequest(portfolio_id=pid, symbol="BTC-USD", side=OrderSide.sell,
                              type=OrderType.limit, qty=D("0.1"), limit_price=D(60000)),
            PlaceOrderRequest(portfolio_id=pid, symbol="BTC-USD", side=OrderSide.sell,
                              type=OrderType.stop, qty=D("0.1"), stop_price=D(45000)),
            now=BOUGHT)
        discard_post_commit(s)
        await s.commit()
    await _plan(sm)
    qs.set_quote("BTC-USD", D(51000), D(51010), D(51005))
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    assert (await _rows(sm))["BTC-USD"].state == "closed"
    async with sm() as s:
        statuses = {o.status for o in await s.scalars(
            select(Order).where(Order.id.in_([o.id for o in legs])))}
    assert statuses == {OrderStatus.canceled}


async def test_a_piano_turned_on_again_leaves_its_earlier_rows_alone(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    gid = await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    await _job(sm, qs, Clock(TUESDAY)).tick()
    assert (await _rows(sm))["AAPL"].state == "grace"
    async with sm() as s:  # off, then on again after the end (plan_since moves on)
        await s.execute(update(Goal).where(Goal.id == gid).values(
            active=True, plan_since=TUESDAY + timedelta(hours=1)))
        await s.commit()
    clock = Clock(DEADLINE - timedelta(minutes=5))
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "grace"  # untouched
    assert await _qty(sm, pid, "AAPL") == 10
    await _buy(sm, qs, pid, "AAPL", 1, now=TUESDAY)  # no longer blocks additions
    assert await _qty(sm, pid, "AAPL") == 11


async def test_a_close_order_ended_unfilled_counts_as_a_failure(sm, qs):
    """The system's close ended without a fill (e.g. rejected by the broker): back to
    grace, counted, the owner told once."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    await _job(sm, qs, Clock(TUESDAY)).tick()
    async with sm() as s:
        filled = await s.scalar(select(Order.id).where(Order.portfolio_id == pid))
    for _ in range(FAIL_NOTICE_AT):
        async with sm() as s:
            await s.execute(update(PlanClosure).values(state="closing", order_id=filled))
            await s.commit()
        await _job(sm, qs, Clock(TUESDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.order_id, row.attempts) == ("grace", None, FAIL_NOTICE_AT)
    assert "close order filled" in row.last_error
    assert sum("non riuscita" in t for t in await _outbox(sm)) == 1


async def test_protection_and_stop_changes_pass_in_grace(sm, qs):
    """No additions in grace, but a PROTEGGI OCO and moving its stop are reductions."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    await _job(sm, qs, Clock(TUESDAY)).tick()
    assert (await _rows(sm))["AAPL"].state == "grace"
    async with sm() as s:
        legs = await place_oco_order(
            s, qs,
            PlaceOrderRequest(portfolio_id=pid, symbol="AAPL", side=OrderSide.sell,
                              type=OrderType.limit, qty=D(10), limit_price=D(220)),
            PlaceOrderRequest(portfolio_id=pid, symbol="AAPL", side=OrderSide.sell,
                              type=OrderType.stop, qty=D(10), stop_price=D(180)),
            now=TUESDAY)
        discard_post_commit(s)
        await s.commit()
    stop = next(o for o in legs if o.type == OrderType.stop)
    async with sm() as s:
        moved = await modify_order(s, qs, stop.id, stop_price=D(185), now=TUESDAY)
        discard_post_commit(s)
        await s.commit()
    assert moved.stop_price == D(185) and moved.status != OrderStatus.rejected
