"""Chiusura dei piani (plan 17; no grace since 20/11/2026): the worker job closes every
position of an ended piano still open at its market's first open session, in profit or in
loss; a closed market waits, a stale or suspect quote is skipped, the kill switch lets the
closes through, a restart does nothing again; the risk check refuses to increase a
position being closed. The week of 11-17 January 2027 ends on Sunday; Monday 18 is a US
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
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import (
    PlaceOrderRequest,
    close_position,
    modify_order,
    place_bracket_order,
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
        self.suspect: str | None = None

    async def get_quotes(self, symbols, *, max_age_s: int = 5):
        quotes = await super().get_quotes(symbols, max_age_s=max_age_s)
        if self.at is None:
            return quotes
        return {k: dataclasses.replace(q, ts=self.at - self.age, suspect=self.suspect)
                for k, q in quotes.items()}


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


async def test_end_closes_every_position_loss_included(sm, qs):
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
    assert {r.grace_days for r in rows.values()} == {0}
    assert rows["BTC-USD"].closed_by == "system"
    assert await _qty(sm, pid, "BTC-USD") == 0
    out = await _outbox(sm)
    assert out[0].startswith("🏁 Piano «settimana» concluso (11/01–17/01): 2 posizioni")
    assert "le chiude SMTrading alla prima seduta utile" in out[0]
    assert "🔒 Piano «settimana»: BTC-USD chiusa da SMTrading (fine piano)" in out[1]
    feed = await _feed(sm)
    assert [t for t, _ in feed] == ["plan.ended", "plan.closed"]
    assert "simul" not in " ".join(t for _, t in feed).lower()

    clock.t = HOLIDAY  # NASDAQ closed: waits
    await _job(sm, qs, clock).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"

    # no additions while being closed
    with pytest.raises(SMTError) as e:
        await _buy(sm, qs, pid, "AAPL", 1, now=HOLIDAY)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert {k: v for k, v in e.value.details.items() if k != "order_id"} == {
        "reason": "plan_grace", "symbol": "AAPL"}
    assert "posizione in chiusura per fine piano" in e.value.message
    async with sm() as s:
        [state] = await plan_states(s)
    assert state["counts"] == {"pending": 1, "closing": 0, "closed": 1}
    assert "grace_days" not in state and "deadline" not in state["positions"][0]

    clock.t = TUESDAY  # the first session: closed in loss, no grace
    await _job(sm, qs, clock).tick()
    aapl = (await _rows(sm))["AAPL"]
    assert (aapl.state, aapl.closed_by, aapl.deadline) == ("closed", "system", None)
    assert await _qty(sm, pid, "AAPL") == 0
    out = await _outbox(sm)
    assert "AAPL chiusa da SMTrading (fine piano)" in out[-1]
    feed = await _feed(sm)
    assert [t for t, _ in feed] == ["plan.ended", "plan.closed", "plan.closed"]
    text = " ".join(out + [t for _, t in feed]).lower()
    assert "grazia" not in text and "grace" not in text

    # a restart sends nothing again and creates nothing
    before = (len(await _outbox(sm)), len(await _feed(sm)))
    await _job(sm, qs, clock).tick()
    assert (len(await _outbox(sm)), len(await _feed(sm))) == before
    async with sm() as s:
        orders = list(await s.scalars(select(Order).where(Order.source == "system")))
    assert len(orders) == 2


async def test_a_position_in_profit_is_closed_at_the_first_session(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _plan(sm)
    qs.set_quote("AAPL", D("210.00"), D("210.10"), D("210.05"))
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    await _job(sm, qs, Clock(HOLIDAY)).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"
    await _job(sm, qs, Clock(TUESDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.closed_by) == ("closed", "system")
    assert await _qty(sm, pid, "AAPL") == 0


async def test_a_suspect_quote_waits_for_a_good_one(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    await _plan(sm)
    qs.set_quote("AAPL", D("150.00"), D("150.10"), D("150.05"))  # a big loss, maybe a bad print
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    qs.suspect = "jump"
    await _job(sm, qs, Clock(TUESDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert row.state == "pending" and "not confirmed" in row.last_error
    assert row.attempts == 0 and await _qty(sm, pid, "AAPL") == 10
    qs.suspect = None
    await _job(sm, qs, Clock(TUESDAY + timedelta(minutes=5))).tick()
    assert (await _rows(sm))["AAPL"].state == "closed"
    assert await _qty(sm, pid, "AAPL") == 0


async def test_a_legacy_grace_row_is_closed_at_the_next_run(sm, qs):
    """A row left in `grace` by the old rule (deadline days away) is closed at once."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    gid = await _plan(sm)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))  # in loss
    async with sm() as s:
        held = await s.get(Position, (pid, "AAPL"))
        s.add(PlanClosure(
            goal_id=gid, period_start=date(2027, 1, 11), period_end=date(2027, 1, 17),
            ended_at=datetime(2027, 1, 17, 23, 0, tzinfo=UTC), portfolio_id=pid,
            symbol="AAPL", qty=D(10), opened_at=held.opened_at, state="grace",
            grace_days=1, attempts=0, deadline=TUESDAY + timedelta(days=3)))
        await s.commit()
    async with sm() as s:
        [state] = await plan_states(s)
    assert state["counts"] == {"pending": 1, "closing": 0, "closed": 0}
    await _job(sm, qs, Clock(TUESDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.closed_by) == ("closed", "system")
    assert await _qty(sm, pid, "AAPL") == 0


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
    assert (await _rows(sm))["AAPL"].state == "pending"
    async with sm() as s:
        await s.execute(update(Goal).where(Goal.id == gid).values(active=False))
        await s.commit()
    await _buy(sm, qs, pid, "AAPL", 1, now=TUESDAY)  # no longer a piano: allowed
    clock.t = TUESDAY
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


async def test_a_triggered_stop_limit_kept_here_never_blocks_the_close(sm, qs):
    """A stop-limit whose stop was hit but whose limit is not reached is a resting limit,
    not a close in flight: the job cancels it (reduce-only too) and closes."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    async with sm() as s:
        sl = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.sell, type=OrderType.stop_limit,
            qty=D(10), stop_price=D(190), limit_price=D("189.50"), reduce_only=True,
            tif=TimeInForce.gtc), now=BOUGHT)
        discard_post_commit(s)
        await s.execute(update(Order).where(Order.id == sl.id)
                        .values(status=OrderStatus.triggered))
        await s.commit()
    await _plan(sm)
    qs.set_quote("AAPL", D("189.00"), D("189.10"), D("189.05"))
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    await _job(sm, qs, Clock(TUESDAY)).tick()
    assert (await _rows(sm))["AAPL"].state == "closed"
    assert await _qty(sm, pid, "AAPL") == 0
    async with sm() as s:
        assert (await s.get(Order, sl.id)).status == OrderStatus.canceled


async def test_a_piano_turned_on_again_leaves_its_earlier_rows_alone(sm, qs):
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    gid = await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"
    async with sm() as s:  # off, then on again after the end (plan_since moves on)
        await s.execute(update(Goal).where(Goal.id == gid).values(
            active=True, plan_since=HOLIDAY))
        await s.commit()
    await _job(sm, qs, Clock(TUESDAY)).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"  # untouched
    assert await _qty(sm, pid, "AAPL") == 10
    await _buy(sm, qs, pid, "AAPL", 1, now=TUESDAY)  # no longer blocks additions
    assert await _qty(sm, pid, "AAPL") == 11


async def test_a_close_order_ended_unfilled_counts_as_a_failure(sm, qs):
    """The system's close ended without a fill (e.g. rejected by the broker): back to
    pending, counted, the owner told once."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    async with sm() as s:
        filled = await s.scalar(select(Order.id).where(Order.portfolio_id == pid))
    for _ in range(FAIL_NOTICE_AT):
        async with sm() as s:
            await s.execute(update(PlanClosure).values(state="closing", order_id=filled))
            await s.commit()
        await _job(sm, qs, Clock(HOLIDAY)).tick()
    row = (await _rows(sm))["AAPL"]
    assert (row.state, row.order_id, row.attempts) == ("pending", None, FAIL_NOTICE_AT)
    assert "close order filled" in row.last_error
    assert sum("non riuscita" in t for t in await _outbox(sm)) == 1


async def test_protection_and_stop_changes_pass_in_plan_closing(sm, qs):
    """No additions while being closed, but a PROTEGGI OCO and moving its stop are
    reductions."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))
    await _plan(sm)
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    assert (await _rows(sm))["AAPL"].state == "pending"
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


# -- resting additions, flips (review fixes) ---------------------------------------------


async def _resting(sm, qs, pid, symbol, side, type, qty, *, now=BOUGHT, **kw):
    async with sm() as s:
        o = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol=symbol, side=side, type=type, qty=D(qty), **kw),
            now=now)
        discard_post_commit(s)
        await s.commit()
        return o.id


async def _status(sm, oid) -> OrderStatus:
    async with sm() as s:
        return (await s.get(Order, oid)).status


async def test_period_end_cancels_the_resting_additions_not_the_exits(sm, qs):
    """Entries placed before the end (a limit buy, a bracket entry with its held exits)
    are canceled by the first run; the exits of the position are kept. Kill switch on."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "AAPL", 10)
    entry = await _resting(sm, qs, pid, "AAPL", OrderSide.buy, OrderType.limit, 5,
                           limit_price=D(150))
    async with sm() as s:
        bracket = await place_bracket_order(
            s, qs, PlaceOrderRequest(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                                     type=OrderType.limit, qty=D(3), limit_price=D(160)),
            take_profit=D(220), stop_loss=D(140), now=BOUGHT)
        discard_post_commit(s)
        await s.commit()
    exit_ = await _resting(sm, qs, pid, "AAPL", OrderSide.sell, OrderType.stop, 10,
                           stop_price=D(170))
    qs.set_quote("AAPL", D("190.00"), D("190.10"), D("190.05"))  # in loss: closed at the open
    await _plan(sm)
    async with sm() as s:
        await set_kill_switch(s, True)
        await s.commit()
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()  # NASDAQ closed: cancels anyway
    assert await _status(sm, entry) == OrderStatus.canceled
    assert {await _status(sm, o.id) for o in bracket} == {OrderStatus.canceled}
    assert await _status(sm, exit_) == OrderStatus.pending
    feed = await _feed(sm)
    canceled = [t for k, t in feed if k == "plan.orders_canceled"]
    assert canceled == [("AAPL: annullati 2 ordini che aumenterebbero la posizione in "
                         "chiusura a fine piano (nessuna aggiunta né inversione)")]
    before = len(await _feed(sm))
    await _job(sm, qs, Clock(HOLIDAY)).tick()  # nothing more to cancel
    assert len(await _feed(sm)) == before


async def _row(sm, pid, symbol, state="pending"):
    """A plan_closures row written directly (the job's cancel pass not run yet)."""
    gid = await _plan(sm)
    async with sm() as s:
        s.add(PlanClosure(
            goal_id=gid, period_start=date(2027, 1, 11), period_end=date(2027, 1, 17),
            ended_at=datetime(2027, 1, 17, 23, 0, tzinfo=UTC), portfolio_id=pid,
            symbol=symbol, qty=D("0.1"), opened_at=None, state=state, grace_days=1,
            attempts=0))
        await s.commit()


async def _processor_tick(sm, qs, symbol, bid, ask, now):
    from smtrading.engine.processor import OrderProcessor

    qs.set_quote(symbol, D(bid), D(ask), D(bid))
    qs.at = now
    try:
        quote = (await qs.get_quotes([symbol]))[symbol]
        await OrderProcessor(sm, None, qs, clock=lambda: now).on_tick(symbol, quote)
    finally:
        qs.at = None


async def test_a_resting_entry_filling_in_plan_closing_is_rejected(sm, qs):
    """Defensive check at fill time: the entry is rejected (reason plan_grace), nothing
    added."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    entry = await _resting(sm, qs, pid, "BTC-USD", OrderSide.buy, OrderType.limit, "0.05",
                           limit_price=D(49000))
    await _row(sm, pid, "BTC-USD")
    await _processor_tick(sm, qs, "BTC-USD", "48900", "48910", TUESDAY)
    async with sm() as s:
        o = await s.get(Order, entry)
        note = await s.scalar(select(Activity).where(Activity.type == "order.rejected"))
    assert (o.status, o.reject_code) == (OrderStatus.rejected, ErrorCode.RISK_LIMIT_EXCEEDED)
    assert "posizione in chiusura per fine piano" in o.reject_message
    assert note.data["reason"] == "plan_grace"
    assert await _qty(sm, pid, "BTC-USD") == D("0.1")


async def test_an_exit_that_would_flip_in_plan_closing_is_rejected(sm, qs):
    """Long 0.1 and a sell stop for 0.15 (not reduce-only): short 0.05 is smaller than the
    long, but a reversal: refused at fill time."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    stop = await _resting(sm, qs, pid, "BTC-USD", OrderSide.sell, OrderType.stop, "0.1",
                          stop_price=D(45000))
    async with sm() as s:
        await s.execute(update(Order).where(Order.id == stop).values(qty=D("0.15")))
        await s.commit()
    await _row(sm, pid, "BTC-USD")
    await _processor_tick(sm, qs, "BTC-USD", "44900", "44910", TUESDAY)
    async with sm() as s:
        o = await s.get(Order, stop)
    assert (o.status, o.reject_code) == (OrderStatus.rejected, ErrorCode.RISK_LIMIT_EXCEEDED)
    assert await _qty(sm, pid, "BTC-USD") == D("0.1")


async def test_a_flip_is_refused_at_placement_in_plan_closing(sm, qs):
    """Long 0.1, sell 0.15 (short 0.05: |after| < |before|) is a reversal: refused; a
    plain reduction passes."""
    pid = await _portfolio(sm)
    await _buy(sm, qs, pid, "BTC-USD", "0.1")
    await _row(sm, pid, "BTC-USD")
    with pytest.raises(SMTError) as e:
        await _buy(sm, qs, pid, "BTC-USD", "0.15", now=TUESDAY, side=OrderSide.sell)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert {k: v for k, v in e.value.details.items() if k != "order_id"} == {
        "reason": "plan_grace", "symbol": "BTC-USD"}
    await _buy(sm, qs, pid, "BTC-USD", "0.05", now=TUESDAY, side=OrderSide.sell)
    assert await _qty(sm, pid, "BTC-USD") == D("0.05")


async def test_total_plan_leaves_out_portfolios_out_of_totals(sm, qs):
    """A total piano covers the portfolios counted in the total only; a portfolio out of
    the totals is closed by its own piano (a custom period here: 2 days)."""
    team = await _portfolio(sm, "Squadra")
    sfida = await _portfolio(sm, "Sfida")
    async with sm() as s:
        await s.execute(update(Portfolio).where(Portfolio.id == sfida).values(in_totals=False))
        await s.commit()
    await _buy(sm, qs, team, "AAPL", 10)
    await _buy(sm, qs, sfida, "AAPL", 3)
    await _plan(sm)
    await _plan(sm, name="sfida", horizon="custom", recurring=False, scope="portfolio",
                portfolio_id=sfida, start_date=date(2027, 1, 16), end_date=date(2027, 1, 17))
    await _job(sm, qs, Clock(SUNDAY_NIGHT)).tick()
    async with sm() as s:
        rows = list(await s.scalars(select(PlanClosure)))
        goals = {g.id: g.name for g in await s.scalars(select(Goal))}
    assert sorted((goals[r.goal_id], r.portfolio_id, r.grace_days) for r in rows) == sorted([
        ("settimana", team, 0), ("sfida", sfida, 0)])
