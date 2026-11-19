"""Reconciliation job (worker, behind the startup gate like the other alerting jobs).

Every TICK_S it decides whether a check of every linked portfolio is due
(`service.check_all`):

- while a stock session is open (SESSION_MARKETS), every INTERVAL;
- once per US session day after its close (NYSE close + AFTER_CLOSE_DELAY), whatever ran
  before.

When it last ran and the day of the after-close run are kept in
`app_settings["reconciliation_job"]` (written before the run: a crashing run is not
retried every tick, a restart does not run again early). The checks themselves are
idempotent across restarts (mismatch streaks from the stored rows, alerts deduplicated on
the activity feed)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.market_hours import local_date, market_status, session_on
from smtrading.reconciliation.service import check_all
from smtrading.risk.models import AppSetting

log = logging.getLogger(__name__)

STATE_KEY = "reconciliation_job"
TICK_S = 60
INTERVAL = timedelta(minutes=15)
AFTER_CLOSE_DELAY = timedelta(minutes=10)
SESSION_MARKETS = ("NYSE", "NASDAQ", "XETRA", "MTA", "EURONEXT_PARIS")
US_MARKET = "NYSE"


def session_open(now: datetime) -> bool:
    for market in SESSION_MARKETS:
        try:
            if market_status(market, now).is_open:
                return True
        except Exception:  # noqa: BLE001, S112  (a calendar problem: the others decide)
            continue
    return False


def after_close_day(now: datetime) -> date | None:
    """The US session day whose after-close check is due at `now`, if any."""
    day = local_date(US_MARKET, now)
    try:
        session = session_on(US_MARKET, day)
    except ValueError:
        return None
    if session is None or now < session[1] + AFTER_CLOSE_DELAY:
        return None
    return day


def due(now: datetime, state: dict) -> tuple[bool, str | None]:
    """(run now, the after-close day it covers or None)."""
    day = after_close_day(now)
    if day is not None and state.get("after_close_day") != day.isoformat():
        return True, day.isoformat()
    if not session_open(now):
        return False, None
    try:
        last = datetime.fromisoformat(str(state.get("last_run_at")))
    except ValueError:
        return True, None
    return now - last >= INTERVAL, None


class ReconciliationJob:
    def __init__(self, sessionmaker: async_sessionmaker, qs: QuoteSource | None, http, *,
                 live_http_factory: Callable | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 tick_s: float = TICK_S) -> None:
        self._sm, self._qs, self._http = sessionmaker, qs, http
        self._live = live_http_factory
        self._clock, self._tick_s = clock, tick_s

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("reconciliation job failed; retrying in %ss", self._tick_s,
                            exc_info=True)
            await asyncio.sleep(self._tick_s)

    async def tick(self) -> bool:
        """Run the checks when due; True when they ran."""
        now = self._clock()
        async with self._sm() as session, session.begin():
            await session.execute(insert(AppSetting).values(key=STATE_KEY, value={})
                                  .on_conflict_do_nothing(index_elements=["key"]))
            row = await session.get(AppSetting, STATE_KEY, with_for_update=True)
            state = dict(row.value) if isinstance(row.value, dict) else {}
            run, day = due(now, state)
            if not run:
                return False
            state["last_run_at"] = now.isoformat()
            if day is not None:
                state["after_close_day"] = day
            row.value = state
        mismatched = await check_all(self._sm, self._qs, http=self._http,
                                     live_http_factory=self._live, clock=self._clock)
        if mismatched:
            log.warning("reconciliation: %s portfolio check(s) in mismatch", mismatched)
        return True
