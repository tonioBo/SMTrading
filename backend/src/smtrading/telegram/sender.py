"""OutboxSender: sends the `pending` outbox rows to the group, one at a time.

Order: the eligible row (`next_attempt_at <= now`) with the lowest id goes first. Errors
that concern the whole chat (Telegram unreachable, 429, 5xx, bad token, bot removed,
group not found) hold every send until the failed row may be retried, and push the
`next_attempt_at` of the other pending rows to that moment too (so the hold survives a
restart): only the head of the queue spends attempts and, when Telegram is back, the
messages arrive in their original order. An error of a single message (any other 400)
backs off that row only; the following ones go on. A 429 never spends an attempt.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.service import write_audit
from smtrading.common.config import get_settings
from smtrading.telegram.client import TelegramClient, TelegramError
from smtrading.telegram.config import TelegramConfig, is_ready, load_config, save_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.quiet import is_quiet

SEND_MIN_INTERVAL_S = 3
MAX_ATTEMPTS = 10
BACKOFF_BASE_S = 5
BACKOFF_MAX_S = 900
DEFAULT_RETRY_AFTER_S = 5
CHAT_NOT_FOUND = "Gruppo non trovato"  # TelegramClient's message for 400 "chat not found"
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_s(attempts: int) -> int:
    """Delay after the `attempts`-th failure: 5, 10, 20, ... capped at 900 s."""
    return min(BACKOFF_BASE_S * 2 ** (attempts - 1), BACKOFF_MAX_S)


def _chat_wide(e: TelegramError) -> bool:
    if e.status == 400:
        return e.message.startswith(CHAT_NOT_FOUND)
    return True  # unreachable (None), 401, 403, 429, 5xx, ...


class OutboxSender:
    def __init__(self, sessionmaker: async_sessionmaker, http: httpx.AsyncClient, *,
                 clock: Callable[[], datetime] = _utcnow, tz: str | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval_s: float = 1.0):
        self._sm = sessionmaker
        self._http = http
        self._clock = clock
        self._tz = tz or get_settings().tz
        self._sleep = sleep
        self._interval_s = interval_s
        self._hold_until: datetime | None = None  # chat-wide pause (429, outage)
        self._last_send: datetime | None = None

    async def run(self) -> None:
        while True:
            if not await self.tick():
                await self._sleep(self._interval_s)

    async def tick(self) -> int:
        """Sends (or attempts) at most one message; returns 1 when a row was handled."""
        if self._hold_until is not None and self._clock() < self._hold_until:
            return 0
        if self._last_send is not None:
            wait = (self._last_send + timedelta(seconds=SEND_MIN_INTERVAL_S)
                    - self._clock()).total_seconds()
            if wait > 0:
                await self._sleep(wait)
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            if not is_ready(cfg) or cfg.paused:
                return 0
            now = self._clock()
            row = await session.scalar(
                select(TelegramOutbox)
                .where(TelegramOutbox.status == OutboxStatus.pending,
                       TelegramOutbox.next_attempt_at <= now)
                .order_by(TelegramOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return 0
            await self._send(session, cfg, row, now)
            return 1

    async def _send(self, session: AsyncSession, cfg: TelegramConfig, row: TelegramOutbox,
                    now: datetime) -> None:
        try:
            client = TelegramClient(cfg.bot_token or "", self._http)
        except TelegramError as e:
            await self._failed(session, row, e, now)
            return
        chat_id = cfg.chat_id or ""
        silent = is_quiet(now, self._tz)
        migrated = False
        while True:
            self._last_send = self._clock()
            try:
                message_id = await client.send_message(
                    chat_id, row.text, entities=row.entities, disable_notification=silent)
            except TelegramError as e:
                if e.migrate_to_chat_id is not None and not migrated:
                    migrated = True
                    chat_id = await self._migrate(session, chat_id, str(e.migrate_to_chat_id))
                    continue  # resend now, no attempt spent
                await self._failed(session, row, e, now)
                return
            break
        row.status = OutboxStatus.sent
        row.sent_at = now
        row.telegram_message_id = message_id
        row.last_error = None
        self._hold_until = None

    async def _migrate(self, session: AsyncSession, old: str, new: str) -> str:
        """The group became a supergroup: the configuration takes the new chat id."""
        fresh = await load_config(session)
        if fresh.chat_id == old:
            fresh.chat_id = new
            await save_config(session, fresh)
        await write_audit(session, agent_id=None, method="admin:telegram.chat_migrated",
                          params={"old": old, "new": new, "by": "system"},
                          outcome="ok", latency_ms=0)
        log.warning("telegram group migrated to a supergroup: chat id updated")
        return new

    async def _failed(self, session: AsyncSession, row: TelegramOutbox, e: TelegramError,
                      now: datetime) -> None:
        row.last_error = e.message[:500]
        if e.status == 429:
            wait = e.retry_after if e.retry_after and e.retry_after > 0 else DEFAULT_RETRY_AFTER_S
            row.next_attempt_at = now + timedelta(seconds=wait)
        else:
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                row.status = OutboxStatus.failed
                log.warning("telegram message %s failed after %s attempts", row.id, row.attempts)
                return
            row.next_attempt_at = now + timedelta(seconds=backoff_s(row.attempts))
        if _chat_wide(e):
            self._hold_until = row.next_attempt_at
            await session.execute(
                update(TelegramOutbox)
                .where(TelegramOutbox.status == OutboxStatus.pending,
                       TelegramOutbox.id != row.id)
                .values(next_attempt_at=func.greatest(TelegramOutbox.next_attempt_at,
                                                      row.next_attempt_at))
            )
