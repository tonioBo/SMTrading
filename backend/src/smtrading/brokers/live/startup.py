"""Startup warning when the `live_fake_broker` setting is on (api and worker).

With it on, every live link talks to the in-memory FakeLiveBroker (e2e only): a
deployment that kept it by mistake would look armed while nothing reaches a broker. Each
process logs a WARNING and queues one Telegram message in the «live» outbox category
(never stale-skipped) once per boot. It is written straight to the outbox, not as an
activity: the agents' activity feed must not hear about it (mcp_hide_mode)."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.activity.models import ActivityLevel
from smtrading.telegram.outbox import LIVE_CATEGORY, enqueue

log = logging.getLogger(__name__)

FAKE_BROKER_TEXT = ("🔴 REALE · ATTENZIONE: broker finto attivo (live_fake_broker) nel"
                    " processo {process}: gli ordini «reali» vanno al broker in memoria, non"
                    " a un broker vero. Solo per i test e2e: disattivalo e riavvia.")
_warned: set[str] = set()


def _fake_on() -> bool:
    from pydantic import ValidationError

    from smtrading.common.config import get_settings

    try:
        return bool(get_settings().live_fake_broker)
    except ValidationError:
        return False


async def warn_if_fake_broker(sessionmaker: async_sessionmaker[AsyncSession], *,
                              process: str) -> bool:
    """WARNING + one «live» Telegram message, once per boot of `process`, when the
    `live_fake_broker` setting is on. True when it is on. Never fails the boot."""
    if not _fake_on():
        return False
    log.warning("live_fake_broker is ON in %s: live links use the in-memory fake broker"
                " (e2e only)", process)
    if process in _warned:
        return True
    _warned.add(process)
    try:
        async with sessionmaker() as session:
            await enqueue(session, source="system", category=LIVE_CATEGORY,
                          level=str(ActivityLevel.critical),
                          text=FAKE_BROKER_TEXT.format(process=process))
            await session.commit()
    except Exception:
        log.warning("live_fake_broker warning not queued for Telegram", exc_info=True)
    return True
