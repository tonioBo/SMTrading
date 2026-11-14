"""TelegramPlanner: turns the activity rows marked `telegram='pending'` into outbox
messages (now), digest entries (quiet hours, non-critical) or `skipped`.

- Real money first (plan 8): a `live.*` row is always queued at once, rendered by
  `brokers.live.protect.render_live_activity`: also while Telegram is not ready (the
  outbox keeps it until it is), unreadable or paused (a `paused` outbox row), at night
  (sent at once, silent: the sender's quiet-hours rule) and however old; never digested,
  never skipped. A direct `order.*` row of a portfolio linked to a live kind is
  `skipped`: its `live.order.*` row carries the message.
- Telegram never configured (no token or no group) or disabled: `skipped`.
- Configuration unreadable (the stored token cannot be decrypted, e.g. a new master key)
  while enabled with a group: nothing is planned, the rows stay `pending` until the token is entered again; then they
  are planned in id order. Non-critical rows older than STALE_AFTER are `skipped` then
  (old news); critical rows are always sent.
- Quiet hours are evaluated when the row is planned; the sender evaluates them again
  when it sends (silent or not).
- Each row is planned in its own savepoint: a row that cannot be planned is marked
  `failed` (and logged) instead of blocking the batch forever.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity, ActivityLevel, TelegramState
from smtrading.agents.models import Agent
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.protect import render_live_activity
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.portfolios.models import Portfolio
from smtrading.telegram.config import TelegramConfig, is_ready, load_config
from smtrading.telegram.outbox import STALE_AFTER, enqueue
from smtrading.telegram.quiet import is_quiet
from smtrading.telegram.render import Names, render_activity

LIVE_PREFIX = "live."

BATCH = 100
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _names(session: AsyncSession, rows: list[Activity]) -> Names:
    agent_ids = {a.agent_id for a in rows if a.agent_id}
    portfolio_ids = {a.portfolio_id for a in rows if a.portfolio_id}
    names = Names()
    if agent_ids:
        names.agents = dict((await session.execute(
            select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))).all())
    if portfolio_ids:
        names.portfolios = dict((await session.execute(
            select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(portfolio_ids)))
        ).all())
    return names


async def _live_portfolios(session: AsyncSession, rows: list[Activity]) -> set:
    """The portfolios of the batch's `order.*` rows that are linked to a live kind."""
    ids = {a.portfolio_id for a in rows if a.portfolio_id and a.type.startswith("order.")}
    if not ids:
        return set()
    return set(await session.scalars(
        select(Portfolio.id)
        .join(BrokerLink, BrokerLink.id == Portfolio.broker_link_id)
        .where(Portfolio.id.in_(ids), BrokerLink.kind.in_(LIVE_KINDS))
    ))


class TelegramPlanner:
    def __init__(self, sessionmaker: async_sessionmaker, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 interval_s: float = 2.0):
        self._sm = sessionmaker
        self._clock = clock
        self._tz = tz or get_settings().tz
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            if await self.tick() < BATCH:
                await asyncio.sleep(self._interval_s)

    async def tick(self) -> int:
        """Plans up to BATCH pending rows (oldest first) in one transaction."""
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            # disabled (or no group) wins over unreadable: skip, never send weeks later
            waiting = cfg.unreadable and cfg.enabled and cfg.chat_id
            stmt = select(Activity).where(Activity.telegram == TelegramState.pending)
            if waiting:  # only the real-money rows go on (to the outbox, where they wait)
                stmt = stmt.where(Activity.type.startswith(LIVE_PREFIX, autoescape=True))
            rows = list(await session.scalars(
                stmt.order_by(Activity.id).limit(BATCH).with_for_update(skip_locked=True)
            ))
            if not rows:
                return 0
            ready = is_ready(cfg)
            quiet = is_quiet(self._clock(), self._tz)
            # ts comes from the database clock: compare it with the same clock
            stale_before = await session.scalar(select(func.now())) - STALE_AFTER
            names = await _names(session, rows)
            live_portfolios = await _live_portfolios(session, rows)
            for a in rows:
                activity_id = a.id  # read before a rollback can expire the row
                try:
                    async with session.begin_nested():
                        if a.type.startswith(LIVE_PREFIX):
                            await self._plan_live(session, cfg, a, names)
                        elif not ready:  # waiting (unreadable) selected live rows only
                            a.telegram = TelegramState.skipped
                        elif a.type.startswith("order.") and a.portfolio_id in live_portfolios:
                            a.telegram = TelegramState.skipped  # its live.order.* row says it
                        else:
                            await self._plan(session, cfg, a, names, quiet, stale_before)
                except Exception:
                    log.exception("telegram planner: activity %s cannot be planned",
                                  activity_id)
                    a.telegram = TelegramState.failed
            return len(rows)

    async def _plan_live(self, session: AsyncSession, cfg: TelegramConfig, a: Activity,
                         names: Names) -> None:
        msg = await enqueue(session, source="system", category="event", level=a.level,
                            text=render_live_activity(a, names, self._tz), cfg=cfg)
        a.telegram = TelegramState.queued
        a.outbox_id = msg.id

    async def _plan(self, session: AsyncSession, cfg: TelegramConfig, a: Activity, names: Names,
                    quiet: bool, stale_before: datetime) -> None:
        if a.level != ActivityLevel.critical and a.ts is not None and a.ts < stale_before:
            a.telegram = TelegramState.skipped
        elif quiet and a.level != ActivityLevel.critical:
            a.telegram = TelegramState.digest
        else:
            msg = await enqueue(session, source="system", category="event", level=a.level,
                                text=render_activity(a, names, self._tz), cfg=cfg)
            a.telegram = TelegramState.queued
            a.outbox_id = msg.id
