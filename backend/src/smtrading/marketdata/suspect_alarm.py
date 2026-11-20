"""Suspect price alarm (worker job, every minute): a symbol with an open order or an open
position (the `orders` / `positions` tracking sets) whose quotes have stayed `suspect`
(marketdata.quality) for more than SUSPECT_ALERT_AFTER_S sends one Telegram warning
(category `system`, never the activity feed the agents read), at most once per symbol
per day (Redis `mdq:alerted:{symbol}:{day}`). Orders and stops on it wait meanwhile."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.marketdata.quality import SUSPECT_ALERT_AFTER_S, suspect_since
from smtrading.marketdata.streaming import _source_key
from smtrading.telegram.outbox import enqueue
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

INTERVAL_S = 60
ALERTED_KEY = "mdq:alerted:{}:{}"


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def alarm_text(symbol: str, minutes: int) -> str:
    return (f"⚠️ Prezzo non affidabile — {symbol}: le quotazioni non sono confermate da "
            f"{minutes} min (salto o fonti in disaccordo). Ordini e stop su {symbol} "
            "aspettano una quotazione verificata: controlla «Salute del sistema».")


class SuspectAlarm:
    def __init__(self, sessionmaker: async_sessionmaker, redis, *,
                 interval_s: float = INTERVAL_S, clock: Callable[[], float] = time.time):
        self._sm = sessionmaker
        self._redis = redis
        self._interval_s = interval_s
        self._clock = clock

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
                await job_tick()
            except Exception:
                log.warning("suspect price check failed; retrying in %ss", self._interval_s,
                            exc_info=True)
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> list[str]:
        """Sends the alarms due; returns their symbols."""
        now = self._clock()
        held: set[str] = set()
        for source in ("orders", "positions"):
            held |= {_s(m) for m in await self._redis.smembers(_source_key(source))}
        since = await suspect_since(self._redis, sorted(held))
        day = datetime.fromtimestamp(now, UTC).astimezone(ZoneInfo(get_settings().tz)).date()
        sent = []
        for symbol, start in sorted(since.items()):
            if now - start <= SUSPECT_ALERT_AFTER_S:
                continue
            key = ALERTED_KEY.format(symbol, day.isoformat())
            if not await self._redis.set(key, "1", nx=True, ex=2 * 86400):
                continue
            try:
                async with self._sm() as session, session.begin():
                    await enqueue(session, source="system", category="system",
                                  level="warning",
                                  text=alarm_text(symbol, int((now - start) // 60)))
            except BaseException:
                await self._redis.delete(key)  # not sent: the next pass tries again
                raise
            log.warning("suspect quotes for %s since %.0f s", symbol, now - start)
            sent.append(symbol)
        return sent
