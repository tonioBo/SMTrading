"""TelegramPlanner: turns the activity rows marked `telegram='pending'` into outbox
messages (now), digest entries (quiet hours, non-critical) or `skipped` (Telegram not
ready). Quiet hours are evaluated when the row is planned; the sender evaluates them
again when it sends (silent or not)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import Activity, ActivityLevel, TelegramState
from smtrading.agents.models import Agent
from smtrading.common.config import get_settings
from smtrading.portfolios.models import Portfolio
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.outbox import enqueue
from smtrading.telegram.quiet import is_quiet
from smtrading.telegram.render import Names, render_activity

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
            rows = list(await session.scalars(
                select(Activity)
                .where(Activity.telegram == TelegramState.pending)
                .order_by(Activity.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            ))
            if not rows:
                return 0
            cfg = await load_config(session)
            if not is_ready(cfg):
                for a in rows:
                    a.telegram = TelegramState.skipped
                return len(rows)
            quiet = is_quiet(self._clock(), self._tz)
            names = await _names(session, rows)
            for a in rows:
                if quiet and a.level != ActivityLevel.critical:
                    a.telegram = TelegramState.digest
                    continue
                msg = await enqueue(session, source="system", category="event",
                                    level=a.level, text=render_activity(a, names, self._tz))
                a.telegram = TelegramState.queued
                a.outbox_id = msg.id
            return len(rows)
