"""The intraday worker job, every TICK_S:

1. one query: the non-archived portfolios with either switch on (`DAY_SCOPE`); none →
   nothing else (no Redis, market data, Paperclip or broker): the live campaign's case;
2. mode A portfolios only: for every (timeframe, market) of their approved intraday
   strategies, the bar that just closed is claimed once in `job_runs` (committed before any
   work: never evaluated twice, across overlapping runs and restarts), evaluated
   (`evaluate_close`, once per market) and its triggers handed to the `Waker`;
3. the safety close (`DayCloser`) of every portfolio in scope;
4. the broker account states (`refresh_account_states`) every ACCOUNT_READ_INTERVAL_S
   while the DAY_TRADE_MARKET session is open, and once in the AFTER_CLOSE_READ after it;
5. SUMMARY_AFTER_CLOSE after that close, one Telegram summary per mode A portfolio with
   triggers in the session (`job_runs`: once).

Each step runs in its own sessions; a failing step is logged and the others still run.
No transaction is held across a network call by this module."""
import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.instruments.market_hours import local_date
from smtrading.instruments.models import Instrument
from smtrading.intraday.account_reader import refresh_account_states
from smtrading.intraday.calendar import due_bar_close, session_window
from smtrading.intraday.day_close import DayCloser
from smtrading.intraday.day_trades import ACCOUNT_READ_INTERVAL_S, DAY_TRADE_MARKET
from smtrading.intraday.models import DAY_SCOPE, INTRADAY_TIMEFRAMES
from smtrading.intraday.summary import day_summary_text
from smtrading.intraday.wake import IssuesFactory, Waker
from smtrading.intraday.watch import evaluate_close
from smtrading.jobs.runs import job_done, run_job_once
from smtrading.marketdata.types import Timeframe
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.signals.service import approved_strategies
from smtrading.telegram.outbox import enqueue
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

TICK_S = 15
BAR_JOB = "intraday_bar"
SUMMARY_JOB = "intraday_summary"
SUMMARY_AFTER_CLOSE = timedelta(minutes=5)
AFTER_CLOSE_READ = timedelta(minutes=10)

ExcludeProvider = Callable[[AsyncSession], Awaitable[frozenset[tuple[uuid.UUID, str]]]]


async def auto_rule_owned_pairs(session: AsyncSession) -> frozenset[tuple[uuid.UUID, str]]:
    """The (portfolio, symbol) pairs owned by an active automatic rule (mode B), which
    mode A must not evaluate."""
    from smtrading.autorules.rules import auto_rule_owned_pairs as owned

    return await owned(session)


class IntradayJob:
    def __init__(self, sessionmaker: async_sessionmaker, router: Any, redis: Any, http: Any, *,
                 issues_factory: IssuesFactory | None = None,
                 clock: Callable[[], datetime] | None = None, tz: str | None = None,
                 tick_s: float = TICK_S,
                 exclude_provider: ExcludeProvider | None = None) -> None:
        self._sm = sessionmaker
        self._router = router
        self._redis = redis
        self._http = http
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tick_s = tick_s
        self._exclude_provider = exclude_provider
        self._waker = Waker(sessionmaker, issues_factory=issues_factory, clock=self._clock,
                            tz=tz)
        self._day_closer = DayCloser(sessionmaker, router, redis, clock=self._clock, tz=tz)
        self._last_account_read: datetime | None = None
        self._after_close_read: datetime | None = None  # the session close already read
        self._summaries_checked: set[str] = set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await job_tick()  # «Salute del sistema»: last pass
            except Exception:
                log.warning("intraday job failed; retrying in %ss", self._tick_s,
                            exc_info=True)
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock()
        async with self._sm() as session:
            portfolios = list(await session.scalars(
                select(Portfolio).where(DAY_SCOPE, Portfolio.status != PortfolioStatus.archived)))
        if not portfolios:
            return
        mode_a = [p for p in portfolios if p.intraday_enabled]
        if mode_a:
            await self._safely("intraday bars", self._bars, mode_a, now)
        await self._safely("intraday day close", self._day_closer.tick, now)
        await self._safely("intraday account states", self._account_states, now)
        if mode_a:
            await self._safely("intraday summaries", self._summaries, mode_a, now)

    @staticmethod
    async def _safely(what: str, fn: Callable[..., Awaitable[Any]], *args: Any) -> None:
        try:
            await fn(*args)
        except Exception:
            log.exception("%s failed", what)

    # -- bar closes (mode A) ---------------------------------------------------------------

    async def _pairs(self, mode_a: list[Portfolio]) -> set[tuple[str, str]]:
        """(timeframe, market) of the approved intraday strategies on mode A portfolios that
        watch that timeframe; no market data read."""
        watching = {p.id: set(p.intraday_timeframes or ()) for p in mode_a}
        wanted: dict[str, set[str]] = {}
        async with self._sm() as session:
            for a in await approved_strategies(session):
                tf = a.timeframe
                if tf not in INTRADAY_TIMEFRAMES or not any(
                        tf in watching.get(p.id, ()) for p in a.portfolios):
                    continue
                for symbol in a.symbols:
                    wanted.setdefault(symbol, set()).add(tf)
            if not wanted:
                return set()
            markets = (await session.execute(
                select(Instrument.symbol, Instrument.market)
                .where(Instrument.symbol.in_(wanted)))).all()
        return {(tf, market) for symbol, market in markets for tf in wanted[symbol]}

    async def _bars(self, mode_a: list[Portfolio], now: datetime) -> None:
        for tf, market in sorted(await self._pairs(mode_a)):
            close = due_bar_close(market, Timeframe(tf), now)
            if close is None:
                continue
            try:
                await self._bar(Timeframe(tf), market, close, now)
            except Exception:
                log.exception("intraday bar %s %s %s failed", tf, market, close.isoformat())

    async def _bar(self, tf: Timeframe, market: str, close: datetime, now: datetime) -> None:
        async with self._sm() as session:
            claimed = await run_job_once(session, BAR_JOB,
                                         f"{tf.value}:{market}:{close.isoformat()}")
            await session.commit()
        if not claimed:
            return
        async with self._sm() as session:
            exclude = (await self._exclude_provider(session) if self._exclude_provider
                       else frozenset())
            triggers = await evaluate_close(session, self._router, self._redis, tf, market,
                                            close, now, exclude=exclude)
        await self._waker.handle(triggers)

    # -- broker account states -------------------------------------------------------------

    async def _account_states(self, now: datetime) -> None:
        window = session_window(DAY_TRADE_MARKET, now)
        if window is None:
            return
        open_, close = window
        if open_ <= now < close:
            last = self._last_account_read
            if last is not None and now - last < timedelta(seconds=ACCOUNT_READ_INTERVAL_S):
                return
            self._last_account_read = now
        elif close <= now < close + AFTER_CLOSE_READ and self._after_close_read != close:
            self._after_close_read = close
        else:
            return
        await refresh_account_states(self._sm, self._http, now)

    # -- end-of-day summary (mode A) -------------------------------------------------------

    async def _summaries(self, mode_a: list[Portfolio], now: datetime) -> None:
        window = session_window(DAY_TRADE_MARKET, now)
        if window is None or now < window[1] + SUMMARY_AFTER_CLOSE:
            return
        day = local_date(DAY_TRADE_MARKET, now)
        for p in mode_a:
            key = f"{p.id}:{day.isoformat()}"
            if key in self._summaries_checked:
                continue
            async with self._sm() as session, session.begin():
                if not await job_done(session, SUMMARY_JOB, key):
                    text = await day_summary_text(session, p, day)
                    if text is not None and await run_job_once(session, SUMMARY_JOB, key):
                        await enqueue(session, source="system", category="system",
                                      level="info", text=text)
            self._summaries_checked.add(key)
