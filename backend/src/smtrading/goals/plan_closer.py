"""The closing of the «piani» (worker job, every 5 minutes, behind the startup gate): the
safety net under the agents, who close every position themselves in the pre-close cycles
of the period's last session, in profit or in loss (the rules are in `goals.plans`; no
grace since 20/11/2026).

Each run:
1. the period of an active piano just over (ended after `plan_since`, within LOOKBACK) is
   claimed once (`job_runs` «plans.ended», goal and period): every position of its
   portfolios open at the end (not opened after it, not already in another piano's
   closing) gets a `plan_closures` row `pending`; one «plan.ended» activity per portfolio
   with positions and one Telegram summary to the owner (category goals);
   then, at every run, each open row (pending, closing) first has the symbol's resting
   orders that would add to the position or turn it (an entry placed before the end, a
   fractional entry renewed every session at the broker; never an exit) canceled
   (`engine.cancel_plan_additions`, «plan.orders_canceled» activity): the fill-time and
   send-time checks refuse them anyway (`goals.plans`);
2. each `pending` row (a legacy `grace` row alike), once its market is open and a good
   quote is at hand, is closed with `engine.close_position` (source system), in profit or
   in loss;
3. a `closing` row is `closed` once the position is flat (its order not filled by the
   end: back to pending, tried again, counted in `attempts` like a refusal).
A row is never closed while another order closing the position is in flight (a market
order, a triggered stop or an order kept for a market-only broker and handed over to be
sent at market, on the closing side, e.g. the agents' CHIUDI queued for the open or still
at the broker; a triggered stop-limit kept here is a resting limit, canceled with the
close): a second full-size close at the broker could sell twice
(BrokerSync clamps a reduce-only order to the position, not to the other open sells);
the row waits for the next run. With the close, `close_position` cancels the symbol's
bracket exits and its other resting orders on the closing side that are not reduce-only
(a PROTEGGI OCO, a stop placed alone): once flat they would open the opposite position,
and at the broker they hold the quantity the close needs.
A position flat (or reopened: another `opened_at`) before SMTrading acts is `closed` by
the agents, with no message. A closed market waits for the open; a missing, stale,
priceless or suspect quote (`marketdata.quality`) skips the row until the next run (never
closed blind). A refused close is rolled back (no rejected order kept), counted in
`attempts` and tried again on the next run; the owner is told once, at FAIL_NOTICE_AT
attempts. Closes are reduce-only, so the kill switch and the halts let them through; a
broker-linked portfolio goes through the normal broker path (the row stays `closing`
until the broker's fill). Every system close sends «plan.closed» (activity) and a
Telegram notice. Rows of a goal no longer an active piano, or of an archived portfolio,
are left alone. Market data is read with no transaction open; every row is handled in
its own transactions."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.mapping import ActivityDraft
from smtrading.activity.models import ActivityLevel
from smtrading.activity.service import record_activity
from smtrading.common.config import get_settings
from smtrading.common.errors import SMTError
from smtrading.engine.models import (
    FILLABLE_STATUSES,
    OPEN_STATUSES,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import cancel_orders, cancel_plan_additions, close_position
from smtrading.events import discard_post_commit, dispatch_post_commit
from smtrading.goals.models import Goal, PlanClosure
from smtrading.goals.plans import ended_at, last_ended_period, open_closures, plan_rows
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.jobs.runs import run_job_once
from smtrading.marketdata.types import Quote
from smtrading.portfolios.locks import lock_live_portfolio
from smtrading.portfolios.models import Portfolio, live_portfolio_ids, total_portfolio_ids
from smtrading.positions.models import Position
from smtrading.risk.settings import get_stale_threshold
from smtrading.telegram.outbox import enqueue
from smtrading.valuation.service import mark_price

log = logging.getLogger(__name__)

PLAN_JOB = "plans.ended"
INTERVAL_S = 300
LOOKBACK = timedelta(days=7)
FAIL_NOTICE_AT = 3
CATEGORY = "goals"


# -- reads shared with the risk check, MCP and the admin API ------------------------------

PLAN_VIEW_STATES = ("pending", "closing", "closed")


def view_state(state: str) -> str:
    """The state shown outside: a legacy `grace` row is pending (closed at the next run)."""
    return "pending" if state == "grace" else state



async def plan_states(session: AsyncSession) -> list[dict[str, Any]]:
    """Per active piano, its latest ended period's positions (the «Obiettivi» page):
    [{goal_id, period_start, period_end, counts {pending, closing, closed}, positions
    [{portfolio_id, portfolio_name, symbol, state, closed_by, closed_at}]}] (a legacy
    `grace` row shows as pending)."""
    goals = list(await session.scalars(
        select(Goal).where(Goal.active.is_(True), Goal.closes_positions.is_(True))))
    out = []
    for goal in goals:
        start = await session.scalar(
            select(PlanClosure.period_start).where(PlanClosure.goal_id == goal.id)
            .order_by(PlanClosure.period_start.desc()).limit(1))
        if start is None:
            continue
        rows = (await session.execute(
            select(PlanClosure, Portfolio.name)
            .join(Portfolio, Portfolio.id == PlanClosure.portfolio_id)
            .where(PlanClosure.goal_id == goal.id, PlanClosure.period_start == start)
            .order_by(Portfolio.name, PlanClosure.symbol))).all()
        counts = dict.fromkeys(PLAN_VIEW_STATES, 0)
        for row, _ in rows:
            counts[view_state(row.state)] += 1
        first = rows[0][0]
        out.append({
            "goal_id": goal.id, "period_start": first.period_start,
            "period_end": first.period_end, "counts": counts,
            "positions": [{"portfolio_id": r.portfolio_id, "portfolio_name": name,
                           "symbol": r.symbol, "state": view_state(r.state),
                           "closed_by": r.closed_by, "closed_at": r.closed_at}
                          for r, name in rows],
        })
    return out


# -- texts (pure) -------------------------------------------------------------------------


def ended_summary(name: str, start, end, symbols: list[str]) -> str:
    if not symbols:
        return f"Piano «{name}» concluso ({start:%d/%m}–{end:%d/%m}): nessuna posizione aperta"
    return (f"Piano «{name}» concluso ({start:%d/%m}–{end:%d/%m}): {len(symbols)} posizioni "
            f"ancora aperte ({', '.join(symbols)}), senza aggiunte: le chiude SMTrading "
            f"alla prima seduta utile del loro mercato, in utile o in perdita")


def closed_summary(name: str, symbol: str) -> str:
    return f"Piano «{name}»: {symbol} chiusa da SMTrading (fine piano)"


# -- the job ------------------------------------------------------------------------------


def _priced(quote: Quote) -> bool:
    try:
        mark_price(quote)
    except SMTError:
        return False
    return True


class PlanCloser:
    def __init__(self, sessionmaker: async_sessionmaker, qs: QuoteSource, redis=None, *,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 interval_s: float = INTERVAL_S) -> None:
        self._sm, self._qs, self._redis = sessionmaker, qs, redis
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(tz or get_settings().tz)
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("plan closer failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        now = self._clock()
        await self._open_periods(now)
        async with self._sm() as session:
            ids = [r.id for r in await open_closures(session)]
        for row_id in ids:
            try:
                await self._additions(row_id)
            except Exception:
                log.warning("plan closing: canceling the additions of row %s failed",
                            row_id, exc_info=True)
            try:
                await self._step(row_id, now)
            except Exception:
                log.warning("plan closing of row %s failed", row_id, exc_info=True)

    # 1. periods over

    async def _covered(self, session: AsyncSession, goal: Goal) -> list[UUID]:
        if goal.scope == "portfolio":
            stmt = live_portfolio_ids().where(Portfolio.id == goal.portfolio_id)
        else:  # a total piano: the portfolios counted in the total
            stmt = total_portfolio_ids()
        return list(await session.scalars(stmt.order_by(Portfolio.name)))

    async def _open_periods(self, now: datetime) -> None:
        today = now.astimezone(self._zone).date()
        async with self._sm() as session, session.begin():
            goals = list(await session.scalars(
                select(Goal).where(Goal.active.is_(True), Goal.closes_positions.is_(True))
                .order_by(Goal.created_at, Goal.id)))
            for goal in goals:
                period = last_ended_period(goal.horizon, goal.start_date, goal.end_date,
                                           goal.recurring, today)
                if period is None or goal.plan_since is None:
                    continue
                end = ended_at(period, self._zone)
                if end <= goal.plan_since or now < end or now - end > LOOKBACK:
                    continue
                if not await run_job_once(session, PLAN_JOB, f"{goal.id}:{period.start}"):
                    continue
                await self._snapshot(session, goal, period, end)

    async def _snapshot(self, session: AsyncSession, goal: Goal, period, end: datetime
                        ) -> None:
        busy = {(r.portfolio_id, r.symbol) for r in await open_closures(session)}
        everything: list[str] = []
        for pid in await self._covered(session, goal):
            positions = list(await session.scalars(
                select(Position).where(Position.portfolio_id == pid, Position.qty != 0)
                .order_by(Position.symbol)))
            symbols = []
            for p in positions:
                if (p.opened_at is not None and p.opened_at >= end) or (pid, p.symbol) in busy:
                    continue
                session.add(PlanClosure(
                    goal_id=goal.id, period_start=period.start, period_end=period.end,
                    ended_at=end, portfolio_id=pid, symbol=p.symbol, qty=p.qty,
                    opened_at=p.opened_at, state="pending", grace_days=0, attempts=0))
                symbols.append(p.symbol)
            if symbols:
                record_activity(session, ActivityDraft(
                    "plan.ended", ActivityLevel.info, False,
                    ended_summary(goal.name, period.start, period.end, symbols),
                    {"goal_name": goal.name, "period_start": period.start,
                     "period_end": period.end, "symbols": symbols}),
                    portfolio_id=pid, agent_id=None)
                everything += symbols
        await enqueue(session, source="system", category=CATEGORY, level="info",
                      text="🏁 " + ended_summary(goal.name, period.start, period.end,
                                                 sorted(set(everything))))

    # 2-3. one row

    async def _load(self, session: AsyncSession, row_id: int):
        row = await session.scalar(plan_rows().where(PlanClosure.id == row_id)
                                   .with_for_update(of=PlanClosure)
                                   .execution_options(populate_existing=True))
        if row is None:
            return None
        goal = await session.get(Goal, row.goal_id)
        instrument = await session.get(Instrument, row.symbol)
        position = await session.get(Position, (row.portfolio_id, row.symbol),
                                     populate_existing=True)
        return row, goal, instrument, position

    async def _step(self, row_id: int, now: datetime) -> None:
        async with self._sm() as session, session.begin():
            loaded = await self._load(session, row_id)
            if loaded is None:
                return
            row, goal, instrument, position = loaded
            if position is None or position.qty == 0 or position.opened_at != row.opened_at:
                await self._closed(session, row, goal, now)
                return
            if row.state == "closing":
                order = await session.get(Order, row.order_id) if row.order_id else None
                if order is None or order.status not in OPEN_STATUSES:
                    row.state, row.order_id = "pending", None
                    status = order.status if order is not None else "missing"
                    await self._failed(session, row, goal,
                                       f"close order {status}, position still open")
                return
            if instrument is None or not market_status(instrument.market, now).is_open:
                return
            threshold = await get_stale_threshold(session, instrument.asset_class)
        quote = await self._quote(row.symbol)  # no transaction open
        if quote is not None and quote.suspect is not None:  # marketdata.quality
            await self._note(row_id, f"quote not confirmed yet ({quote.suspect})", now,
                             notify=False)
            return  # retried every pass; the guard accepts a lasting level in bounded time
        if quote is None or (now - quote.ts).total_seconds() > threshold:
            await self._note(row_id, "quote missing or not fresh", now, notify=False)
            return
        if not _priced(quote):
            await self._note(row_id, "quote has no price", now, notify=False)
            return
        await self._close(row_id, now)

    async def _quote(self, symbol: str) -> Quote | None:
        try:
            return (await self._qs.get_quotes([symbol])).get(symbol)
        except SMTError:
            return None
        except Exception:
            log.warning("plan closing: quote of %s failed", symbol, exc_info=True)
            return None

    async def _closed(self, session: AsyncSession, row: PlanClosure, goal: Goal,
                      now: datetime) -> None:
        by = "system" if row.state == "closing" else "agent"
        row.state, row.closed_by, row.closed_at = "closed", by, now
        if by == "system":
            await self._announce_close(session, row, goal)

    async def _announce_close(self, session: AsyncSession, row: PlanClosure,
                              goal: Goal) -> None:
        record_activity(session, ActivityDraft(
            "plan.closed", ActivityLevel.info, False,
            closed_summary(goal.name, row.symbol),
            {"goal_name": goal.name, "symbol": row.symbol, "by": "system",
             "order_id": row.order_id}),
            portfolio_id=row.portfolio_id, agent_id=None)
        name = await session.scalar(select(Portfolio.name)
                                    .where(Portfolio.id == row.portfolio_id))
        await enqueue(session, source="system", category=CATEGORY, level="info",
                      text=f"🔒 {closed_summary(goal.name, row.symbol)} · {name}")

    async def _note(self, row_id: int, error: str, now: datetime, *, notify: bool) -> None:
        async with self._sm() as session, session.begin():
            loaded = await self._load(session, row_id)
            if loaded is None:
                return
            row, goal, _, _ = loaded
            if not notify:
                row.last_error = error[:300]
                return
            await self._failed(session, row, goal, error)

    async def _failed(self, session: AsyncSession, row: PlanClosure, goal: Goal,
                      error: str) -> None:
        """A close that did not happen: counted, the owner told once at FAIL_NOTICE_AT."""
        row.last_error = error[:300]
        row.attempts = (row.attempts or 0) + 1
        if row.attempts == FAIL_NOTICE_AT:
            name = await session.scalar(select(Portfolio.name)
                                        .where(Portfolio.id == row.portfolio_id))
            await enqueue(session, source="system", category=CATEGORY, level="warning",
                          text=(f"⚠️ Piano «{goal.name}»: chiusura di {row.symbol} · "
                                f"{name} non riuscita ({error[:120]}); nuovo tentativo "
                                f"a ogni passaggio"))

    async def _close_in_flight(self, session: AsyncSession, row: PlanClosure,
                               held: Decimal) -> bool:
        """Another order closing the position is in flight (module docstring)."""
        side = OrderSide.sell if held > 0 else OrderSide.buy
        return await session.scalar(
            select(Order.id).where(
                Order.portfolio_id == row.portfolio_id, Order.symbol == row.symbol,
                Order.side == side, Order.status.in_(FILLABLE_STATUSES),
                (Order.type == OrderType.market)
                | (Order.status == OrderStatus.partially_filled)
                # a fired stop is a market order; a triggered stop-limit kept here is a
                # resting limit (canceled with the close, `_local_triggered`)
                | ((Order.status == OrderStatus.triggered)
                   & ((Order.type != OrderType.stop_limit)
                      | Order.broker_link_id.is_not(None)))
                # plan 29: a synthetic order handed over is sent at market
                | (Order.synthetic.is_(True) & Order.broker_link_id.is_not(None)))
            .limit(1)) is not None

    async def _cancel_local_triggered(self, session: AsyncSession, row: PlanClosure,
                                      held: Decimal) -> None:
        """The closing side's triggered stop-limits kept here (their stop was hit, their
        limit not reached yet): canceled with the close, reduce-only or not, so they never
        sell what the close sells (the portfolio is locked)."""
        side = OrderSide.sell if held > 0 else OrderSide.buy
        orders = list(await session.scalars(
            select(Order).where(
                Order.portfolio_id == row.portfolio_id, Order.symbol == row.symbol,
                Order.side == side, Order.status == OrderStatus.triggered,
                Order.type == OrderType.stop_limit, Order.broker_link_id.is_(None))
            .with_for_update().execution_options(populate_existing=True)))
        if orders:
            await cancel_orders(session, orders, by="system")

    async def _additions(self, row_id: int) -> None:
        async with self._sm() as session:
            try:
                async with session.begin():
                    loaded = await self._load(session, row_id)
                    if loaded is None:
                        return
                    row, _, _, position = loaded
                    if (position is None or position.qty == 0
                            or position.opened_at != row.opened_at):
                        return  # the step marks it closed
                    await self._cancel_additions(session, row)
            except BaseException:
                discard_post_commit(session)
                raise
            await dispatch_post_commit(session, self._redis)

    async def _cancel_additions(self, session: AsyncSession, row: PlanClosure) -> None:
        """The symbol's resting orders that would add to the position (module docstring);
        the portfolio is locked only when there are candidates."""
        def candidates():
            return (select(Order).where(
                Order.portfolio_id == row.portfolio_id, Order.symbol == row.symbol,
                Order.status.in_(FILLABLE_STATUSES), Order.reduce_only.is_not(True))
                .order_by(Order.created_at, Order.id))
        if await session.scalar(candidates().with_only_columns(Order.id).limit(1)) is None:
            return
        await lock_live_portfolio(session, row.portfolio_id)
        orders = list(await session.scalars(
            candidates().with_for_update(of=Order)
            .execution_options(populate_existing=True)))
        await cancel_plan_additions(session, orders)

    async def _close(self, row_id: int, now: datetime) -> None:
        async with self._sm() as session:
            try:
                async with session.begin():
                    loaded = await self._load(session, row_id)
                    if loaded is None or loaded[0].state not in ("pending", "grace"):
                        return
                    row, goal, _, _ = loaded
                    await lock_live_portfolio(session, row.portfolio_id)
                    held = await session.get(Position, (row.portfolio_id, row.symbol),
                                             populate_existing=True)
                    if held is None or held.qty == 0:
                        return  # flat meanwhile: the next run marks it closed
                    if await self._close_in_flight(session, row, held.qty):
                        row.last_error = "another close order is in flight"
                        return
                    await self._cancel_local_triggered(session, row, held.qty)
                    order = await close_position(
                        session, self._qs, row.portfolio_id, row.symbol, source="system",
                        now=now, redis=self._redis)
                    row.state, row.order_id, row.last_error = "closing", order.id, None
                    position = await session.get(Position, (row.portfolio_id, row.symbol),
                                                 populate_existing=True)
                    if position is None or position.qty == 0:
                        row.state, row.closed_by, row.closed_at = "closed", "system", now
                        await self._announce_close(session, row, goal)
            except SMTError as e:
                discard_post_commit(session)
                log.warning("plan closing of %s refused: %s", row_id, e.message)
                await self._note(row_id, f"{e.code}: {e.message}", now, notify=True)
                return
            except BaseException:
                discard_post_commit(session)
                raise
            await dispatch_post_commit(session, self._redis)

