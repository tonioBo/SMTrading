"""LiveMonitor: the worker loop that watches every real-money link.

Every LIVE_MONITOR_TICK_S it reconciles (brokers.live.reconcile.reconcile_live) each link
with a `live_state` whose last reconciliation is LIVE_RECONCILE_INTERVAL_S old, or whose
portfolio got a fill after it. Trading 212: never more than one account read every
T212_ACCOUNT_MIN_INTERVAL_S (the BrokerSync cash recheck reads it too: `last_snapshot_at`).

Health: a failed broker call increments `health_failures`, a success resets it; at the
HEALTH_FAILURES_ALERT-th consecutive failure a critical `live.unreachable` is sent, once
until the broker is reachable again. Credentials refused (HTTP 401/403) count as failures
too, with their own critical `live.credentials_rejected` («credenziali rifiutate») at the
first one, once until a success. A broker rate limit (HTTP 429) is not a failure: the
link is tried again at its next turn; one lasting RATE_LIMIT_ALERT_AFTER sends one
critical `live.unreachable` («limite di frequenza del broker…») per episode. A failure of the monitor itself (not the broker:
the database, a bug) is logged, the link waits its next turn, and at the
HEALTH_FAILURES_ALERT-th in a row a critical `live.monitor_failed` is sent (once until a
success); it neither starts nor ends a rate-limit episode (no broker answer was
seen). The link's live_state never changes for health. The per-link state (last pass,
failure counts, alert flags) is dropped for links no longer live and enabled, and the
failure/alert state also when no portfolio is linked any more.

Every tick first retries the settlement of a disarm whose phase 2 failed
(service.settle_disarm_leftovers: a `configured` link whose portfolios still have orders
from the armed period), alerted once per link until nothing is left on it.

Single writer: `run` holds a Postgres advisory lock on a dedicated connection (as
BrokerSync); a worker that cannot take it skips its ticks. Broker I/O never happens
with a transaction open.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.protect import live_alert
from smtrading.brokers.live.reconcile import (
    LIVE_RECONCILE_INTERVAL_S,
    LiveBrokerRateLimited,
    LiveBrokerUnreachable,
    reconcile_live,
)
from smtrading.brokers.live.service import settle_disarm_leftovers
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill
from smtrading.engine.quotesource import QuoteSource
from smtrading.portfolios.locks import lock_broker_link
from smtrading.portfolios.models import Portfolio

log = logging.getLogger(__name__)

LIVE_MONITOR_TICK_S = 5
HEALTH_FAILURES_ALERT = 3
T212_ACCOUNT_MIN_INTERVAL_S = 5
ADVISORY_LOCK_KEY = 0x534D544C564D  # "SMTLVM"
UNREACHABLE_SUMMARY = "broker non raggiungibile"
CREDENTIALS_SUMMARY = "credenziali rifiutate dal broker: controlla le chiavi"
RATE_LIMIT_ALERT_AFTER = timedelta(minutes=15)
RATE_LIMIT_SUMMARY = "limite di frequenza del broker da oltre 15 minuti: conto non verificato"
MONITOR_FAILED_SUMMARY = "controllo del conto reale non riuscito (errore interno)"


class LiveMonitor:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], *,
        http_factory: Callable | None = None, qs: QuoteSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sm = sessionmaker
        self._http_factory = http_factory or (
            lambda link_id: make_live_client(sessionmaker, link_id))
        self._qs = qs
        self._clock = clock
        self._sleep = sleep
        self._last: dict[UUID, tuple[datetime, datetime]] = {}  # link -> (clock, db time)
        self._internal: dict[UUID, int] = {}  # consecutive monitor (non-broker) failures
        self._auth_alerted: set[UUID] = set()
        self._rate_limited: dict[UUID, tuple[datetime, bool]] = {}  # since, alerted
        self._settle_alerted: set[UUID] = set()  # auto-settled disarms, alerted once

    async def run(self) -> None:
        engine = self._sm.kw["bind"]
        warned = False
        while True:
            try:
                async with engine.connect() as conn:
                    try:
                        held = False
                        while True:
                            if not held:
                                held = bool(await conn.scalar(
                                    text("select pg_try_advisory_lock(:k)"),
                                    {"k": ADVISORY_LOCK_KEY}))
                                await conn.commit()
                                if not held and not warned:
                                    log.warning("live monitor: another worker holds the lock")
                                    warned = True
                            else:
                                await conn.scalar(text("select 1"))  # still holding it
                                await conn.commit()
                            if held:
                                warned = False
                                try:
                                    await self.tick()
                                except Exception:
                                    log.exception("live monitor tick failed")
                            await self._sleep(LIVE_MONITOR_TICK_S)
                    finally:
                        with contextlib.suppress(Exception):
                            await conn.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("live monitor: lock connection lost; reconnecting")
                await self._sleep(LIVE_MONITOR_TICK_S)

    async def tick(self) -> int:
        """Settle the leftovers of failed disarms, then reconcile the due live links;
        returns how many were reconciled."""
        await self._settle_disarms()
        now = self._clock()
        async with self._sm() as session:
            db_now = await session.scalar(select(func.now()))
            last_fill = (
                select(func.max(Fill.created_at))
                .join(Portfolio, Portfolio.id == Fill.portfolio_id)
                .where(Portfolio.broker_link_id == BrokerLink.id)
                .scalar_subquery()
            )
            links = (await session.execute(
                select(BrokerLink.id, BrokerLink.kind, BrokerLink.last_snapshot_at, last_fill)
                .where(BrokerLink.live_state.is_not(None), BrokerLink.enabled.is_(True))
                .order_by(BrokerLink.name)
            )).all()
        self._prune({row[0] for row in links})
        done = 0
        for link_id, kind, snapshot_at, fill_at in links:
            if not self._due(link_id, kind, now, db_now, snapshot_at, fill_at):
                continue
            try:
                row = await reconcile_live(self._sm, link_id, self._http_factory, qs=self._qs)
            except LiveBrokerRateLimited as e:  # not a health failure: next turn
                self._last[link_id] = (now, db_now)
                log.warning("live link %s: broker rate limit: %s", link_id, e)
                await self._rate_limit(link_id, now)
                continue
            except LiveBrokerUnreachable as e:
                self._rate_limited.pop(link_id, None)
                self._last[link_id] = (now, db_now)
                self._internal.pop(link_id, None)
                await self._health(link_id, error=str(e), auth=e.auth)
                continue
            except Exception:
                log.exception("live reconciliation of link %s failed", link_id)
                self._last[link_id] = (now, db_now)  # its next turn, not every tick
                await self._internal_failure(link_id)
                continue
            self._last[link_id] = (now, db_now)
            self._internal.pop(link_id, None)
            self._rate_limited.pop(link_id, None)
            if row is not None:
                done += 1
                await self._health(link_id, error=None)
            else:  # no portfolio linked (no broker call): a new episode if linked again
                self._auth_alerted.discard(link_id)
        return done

    async def _settle_disarms(self) -> None:
        """Automatic retry of a failed disarm settlement (service.settle_disarm_leftovers):
        one alert per link until a pass finds nothing left to settle on it."""
        def first_time(link_id: UUID) -> bool:
            if link_id in self._settle_alerted:
                return False
            self._settle_alerted.add(link_id)
            return True

        try:
            settled = await settle_disarm_leftovers(self._sm, alert=first_time)
        except Exception:
            log.exception("live monitor: disarm settlement check failed")
            return
        self._settle_alerted &= set(settled)

    def _prune(self, live: set[UUID]) -> None:
        """Forget the links no longer live and enabled (unlinked, disabled, deleted)."""
        for state in (self._last, self._internal, self._rate_limited):
            for link_id in [k for k in state if k not in live]:
                del state[link_id]
        self._auth_alerted &= live

    async def _rate_limit(self, link_id: UUID, now: datetime) -> None:
        since, alerted = self._rate_limited.setdefault(link_id, (now, False))
        if alerted or now - since <= RATE_LIMIT_ALERT_AFTER:
            return
        self._rate_limited[link_id] = (since, True)
        try:
            async with self._sm() as session:
                await live_alert(session, kind="unreachable", summary=RATE_LIMIT_SUMMARY,
                                 portfolio_id=await self._portfolio_of(session, link_id),
                                 link_id=link_id,
                                 data={"rate_limited_since": since.isoformat()})
                await session.commit()
        except Exception:
            log.exception("live monitor: alert for link %s failed", link_id)

    async def _internal_failure(self, link_id: UUID) -> None:
        count = self._internal[link_id] = self._internal.get(link_id, 0) + 1
        if count != HEALTH_FAILURES_ALERT:
            return
        try:
            async with self._sm() as session:
                await live_alert(session, kind="monitor_failed", summary=MONITOR_FAILED_SUMMARY,
                                 portfolio_id=await self._portfolio_of(session, link_id),
                                 link_id=link_id, data={"failures": count})
                await session.commit()
        except Exception:
            log.exception("live monitor: alert for link %s failed", link_id)

    @staticmethod
    async def _portfolio_of(session: AsyncSession, link_id: UUID) -> UUID | None:
        return await session.scalar(
            select(Portfolio.id).where(Portfolio.broker_link_id == link_id)
            .order_by(Portfolio.id).limit(1))

    def _due(self, link_id: UUID, kind: str, now: datetime, db_now: datetime,
             snapshot_at: datetime | None, fill_at: datetime | None) -> bool:
        if (kind == "t212_live" and snapshot_at is not None
                and (db_now - snapshot_at).total_seconds() < T212_ACCOUNT_MIN_INTERVAL_S):
            return False  # at most one account read every 5 s
        last = self._last.get(link_id)
        if last is None:
            return True
        last_clock, last_db = last
        if (now - last_clock).total_seconds() >= LIVE_RECONCILE_INTERVAL_S:
            return True
        return fill_at is not None and fill_at > last_db

    async def _health(self, link_id: UUID, *, error: str | None, auth: bool = False) -> None:
        async with self._sm() as session:
            link = await lock_broker_link(session, link_id)  # FOR NO KEY UPDATE (R8.9)
            if link is None:
                return
            if error is None:
                link.health_failures, link.status, link.last_error = 0, "ok", None
                self._auth_alerted.discard(link_id)
            else:
                link.health_failures = (link.health_failures or 0) + 1
                link.status, link.last_error = "error", error[:500]
                log.warning("live link %s unreachable (%s in a row): %s", link_id,
                            link.health_failures, error)
                if auth and link_id not in self._auth_alerted:
                    self._auth_alerted.add(link_id)
                    await live_alert(session, kind="credentials_rejected",
                                     summary=CREDENTIALS_SUMMARY,
                                     portfolio_id=await self._portfolio_of(session, link_id),
                                     link_id=link_id, data={"error": error[:300]})
                elif not auth and link.health_failures == HEALTH_FAILURES_ALERT:
                    await live_alert(session, kind="unreachable", summary=UNREACHABLE_SUMMARY,
                                     portfolio_id=await self._portfolio_of(session, link_id),
                                     link_id=link_id, data={"failures": link.health_failures})
            await session.commit()
