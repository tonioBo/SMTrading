"""LiveMonitor: the worker loop that watches every real-money link.

Every LIVE_MONITOR_TICK_S it reconciles (brokers.live.reconcile.reconcile_live) each link
with a `live_state` whose last reconciliation is LIVE_RECONCILE_INTERVAL_S old, or whose
portfolio got a fill after it. Trading 212: never more than one account read every
T212_ACCOUNT_MIN_INTERVAL_S (the BrokerSync cash recheck reads it too: `last_snapshot_at`).

Health: a failed broker call increments `health_failures`, a success resets it; at the
HEALTH_FAILURES_ALERT-th consecutive failure a critical `live.unreachable` is sent, once
until the broker is reachable again. The link's live_state never changes for health.

Single writer: `run` holds a Postgres advisory lock on a dedicated connection (as
BrokerSync); a worker that cannot take it skips its ticks. Broker I/O never happens
with a transaction open.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.protect import live_alert
from smtrading.brokers.live.reconcile import (
    LIVE_RECONCILE_INTERVAL_S,
    LiveBrokerUnreachable,
    reconcile_live,
)
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import Fill
from smtrading.engine.quotesource import QuoteSource
from smtrading.portfolios.models import Portfolio

log = logging.getLogger(__name__)

LIVE_MONITOR_TICK_S = 5
HEALTH_FAILURES_ALERT = 3
T212_ACCOUNT_MIN_INTERVAL_S = 5
ADVISORY_LOCK_KEY = 0x534D544C564D  # "SMTLVM"
UNREACHABLE_SUMMARY = "broker non raggiungibile"


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
        """Reconcile the due live links; returns how many were reconciled."""
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
        done = 0
        for link_id, kind, snapshot_at, fill_at in links:
            if not self._due(link_id, kind, now, db_now, snapshot_at, fill_at):
                continue
            try:
                row = await reconcile_live(self._sm, link_id, self._http_factory, qs=self._qs)
            except LiveBrokerUnreachable as e:
                self._last[link_id] = (now, db_now)
                await self._health(link_id, error=str(e))
                continue
            except Exception:
                log.exception("live reconciliation of link %s failed", link_id)
                continue
            self._last[link_id] = (now, db_now)
            if row is not None:
                done += 1
                await self._health(link_id, error=None)
        return done

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

    async def _health(self, link_id: UUID, *, error: str | None) -> None:
        async with self._sm() as session:
            link = await session.get(BrokerLink, link_id, with_for_update=True)
            if link is None:
                return
            if error is None:
                link.health_failures, link.status, link.last_error = 0, "ok", None
            else:
                link.health_failures = (link.health_failures or 0) + 1
                link.status, link.last_error = "error", error[:500]
                log.warning("live link %s unreachable (%s in a row): %s", link_id,
                            link.health_failures, error)
                if link.health_failures == HEALTH_FAILURES_ALERT:
                    portfolio_id = await session.scalar(
                        select(Portfolio.id).where(Portfolio.broker_link_id == link_id)
                        .order_by(Portfolio.id).limit(1))
                    await live_alert(session, kind="unreachable", summary=UNREACHABLE_SUMMARY,
                                     portfolio_id=portfolio_id, link_id=link_id,
                                     data={"failures": link.health_failures})
            await session.commit()
