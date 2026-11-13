"""The worker's job loop: every TICK_S it runs whatever is due.

- interest of the day just ended, at (or after) 00:00 UTC;
- corporate actions of today, at (or after) 06:00 UTC;
- every minute: lift expired daily-loss blocks, `day_open` snapshot of every portfolio;
- every 60s: margin sweep (margin and daily loss checks);
- every 5 minutes: periodic equity snapshots and, with an http client, the broker
  reconciliation of every enabled broker link.

Every job has its own transactions and commits; a failure is logged and the loop goes on.
Daily jobs are idempotent through job_runs and retried every DAILY_RETRY_S until done.

Catch-up (I6): a daily job also runs, oldest first and before the current day, every
day of the previous CATCH_UP_DAYS that is not marked done in job_runs, logged as an
ERROR. Only days after the first day the job ever completed count as missed, so a new
installation does not back-charge the week before it existed.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.sync import reconcile_all
from smtrading.corporate.actions import run_corporate_actions
from smtrading.corporate.interest import run_interest
from smtrading.corporate.snapshots import ensure_day_open, take_snapshots
from smtrading.engine.quotesource import QuoteSource
from smtrading.jobs.runs import job_done
from smtrading.portfolios.models import Portfolio
from smtrading.risk.daily_loss import reset_daily
from smtrading.risk.margin import margin_sweep
from smtrading.risk.models import JobRun

log = logging.getLogger("smtrading.jobs.scheduler")

TICK_S = 30
INTEREST_AT = time(0)  # UTC
CORPORATE_AT = time(6)  # UTC
DAY_OPEN_INTERVAL_S = 60
MARGIN_SWEEP_INTERVAL_S = 60
SNAPSHOT_INTERVAL_S = 300
RECONCILE_INTERVAL_S = 300
DAILY_RETRY_S = 300
CATCH_UP_DAYS = 7

DailyJob = Callable[[AsyncSession, QuoteSource, date], Awaitable[int]]


class JobScheduler:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], qs: QuoteSource, redis, *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC), tick_s: float = TICK_S,
        http=None,
    ) -> None:
        self._sm = sessionmaker
        self._http = http
        self._qs = qs
        self._redis = redis
        self._clock = clock
        self._tick_s = tick_s
        self._last_run: dict[str, datetime] = {}
        self._done: set[tuple[str, str]] = set()  # (daily job, day key) completed
        self._caught_up: dict[str, date] = {}  # daily job -> day with no missed days before

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> None:
        now = self._clock().astimezone(UTC)
        await self._every("day open", DAY_OPEN_INTERVAL_S, now, self._day_open)
        await self._daily("interest", INTEREST_AT, now, now.date() - timedelta(days=1),
                          run_interest)
        await self._daily(
            "corporate", CORPORATE_AT, now, now.date(),
            lambda session, qs, day: run_corporate_actions(session, qs, day, redis=self._redis),
        )
        await self._every(
            "margin sweep", MARGIN_SWEEP_INTERVAL_S, now,
            lambda: margin_sweep(self._sm, self._qs, self._redis, clock=self._clock),
        )
        await self._every(
            "equity snapshots", SNAPSHOT_INTERVAL_S, now,
            lambda: take_snapshots(self._sm, self._qs, clock=self._clock),
        )
        if self._http is not None:
            await self._every(
                "broker reconciliation", RECONCILE_INTERVAL_S, now,
                lambda: reconcile_all(self._sm, self._http),
            )

    async def _every(
        self, name: str, interval_s: float, now: datetime, job: Callable[[], Awaitable[None]]
    ) -> None:
        last = self._last_run.get(name)
        if last is not None and (now - last).total_seconds() < interval_s:
            return
        self._last_run[name] = now
        try:
            await job()
        except Exception:
            log.exception("%s failed", name)

    async def _daily(
        self, job: str, at: time, now: datetime, day: date, run: DailyJob
    ) -> None:
        """The missed days of `job` (catch-up, oldest first), then `day` once `at` is past."""
        for missed in await self._missed_days(job, day):
            await self._run_day(job, missed, now, run, catch_up=True)
        if now.time() >= at:
            await self._run_day(job, day, now, run, catch_up=False)

    async def _missed_days(self, job: str, day: date) -> list[date]:
        """Days of the CATCH_UP_DAYS before `day` not marked done, after the first day
        the job ever completed (none for a job that never ran)."""
        if self._caught_up.get(job) == day:
            return []
        try:
            async with self._sm() as session:
                keys = list(await session.scalars(select(JobRun.key).where(JobRun.job == job)))
        except Exception:
            log.exception("%s: reading the completed days failed", job)
            return []
        done: set[date] = set()
        for key in keys:
            try:
                done.add(date.fromisoformat(key))
            except ValueError:
                continue  # per-portfolio claims like "{day}:{portfolio_id}"
        missed = [] if not done else [
            d for d in (day - timedelta(days=n) for n in range(CATCH_UP_DAYS, 0, -1))
            if d > min(done) and d not in done
        ]
        if not missed:
            self._caught_up[job] = day
        return missed

    async def _run_day(
        self, job: str, day: date, now: datetime, run: DailyJob, *, catch_up: bool
    ) -> None:
        key = day.isoformat()
        if (job, key) in self._done:
            return
        attempt = f"{job}:{key}"
        last = self._last_run.get(attempt)
        if last is not None and (now - last).total_seconds() < DAILY_RETRY_S:
            return
        self._last_run[attempt] = now
        if catch_up:
            log.error("%s %s was missed: catching up", job, key)
        try:
            async with self._sm() as session:
                journals = await run(session, self._qs, day)
                done = await job_done(session, job, key)
            if journals:
                log.info("%s %s: %s journals", job, key, journals)
            if done:
                self._done.add((job, key))
        except Exception:
            log.exception("%s %s failed", job, key)

    async def _day_open(self) -> None:
        """Lift expired daily-loss blocks; `day_open` snapshot of every portfolio."""
        async with self._sm() as session:
            await reset_daily(session, self._clock())
            await session.commit()
            ids = list(await session.scalars(select(Portfolio.id).order_by(Portfolio.id)))
        for portfolio_id in ids:
            try:
                async with self._sm() as session:
                    portfolio = await session.get(Portfolio, portfolio_id)
                    await ensure_day_open(session, self._qs, portfolio, self._clock())
                    await session.commit()
            except Exception:
                log.exception("day_open snapshot of portfolio %s failed", portfolio_id)
