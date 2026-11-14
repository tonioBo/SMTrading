"""OutboxSender: sends the `pending` outbox rows to the group, one at a time.

Single sender: `run` holds a Postgres advisory lock on a dedicated connection (as
BrokerSync does); a worker that cannot take it sends nothing.

Order: the due row (`next_attempt_at <= now`) with the lowest id goes first.

Errors (`TelegramError.kind`):
- transient, for the whole chat (no response, 5xx, 429): every send is held until the
  failed row may be retried, and the `next_attempt_at` of the other pending rows is pushed
  to that moment too (so the hold survives a restart). Only the head of the queue is
  retried and, when Telegram is back, the messages arrive in their original order. Such
  an error never makes a row `failed`: attempts are counted (for the admin page) and the
  backoff stays at its 900 s cap. A 429 waits `retry_after` and spends no attempt.
- chat, until the configuration is fixed (bad token, bot removed or without rights,
  group not found): same hold, attempts counted; after MAX_ATTEMPTS the head is `failed`
  (it can be retried) and the hold stays, so the next row waits its turn.
- message (any other error, e.g. a 400 on the entities): that row only backs off and,
  after MAX_ATTEMPTS, is `failed`; the following rows go on.

A restarted sender recovers the hold from the head row: lowest-id pending row not due and
whose last error was chat-wide. A held sender polls the settings' `hold_version`: a new
token or group, a resume or «Riprova tutti i falliti» (`telegram.settings.release_hold`)
bumps it, and the hold is dropped at once instead of waiting up to 900 s.

Supergroup migration (400 with `migrate_to_chat_id`): the configuration takes the new
chat id (audited when it actually changes), and the message is sent again at once to the
new id, in the same tick and with no attempt spent: the old id will never accept it.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.agents.service import write_audit
from smtrading.common.config import get_settings
from smtrading.telegram.client import (
    CHAT,
    MESSAGE,
    TRANSIENT,
    TelegramClient,
    TelegramError,
    kind_of_message,
)
from smtrading.telegram.config import (
    TelegramConfig,
    is_ready,
    load_config,
    load_hold_version,
    save_config,
)
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.quiet import is_quiet
from smtrading.telegram.settings import lock_settings

SEND_MIN_INTERVAL_S = 3
MAX_ATTEMPTS = 10
BACKOFF_BASE_S = 5
BACKOFF_MAX_S = 900
DEFAULT_RETRY_AFTER_S = 5
ADVISORY_LOCK_KEY = 0x534D54544753  # "SMTTGS"
log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_s(attempts: int) -> int:
    """Delay after the `attempts`-th failure: 5, 10, 20, ... capped at 900 s."""
    return min(BACKOFF_BASE_S * 2 ** (min(attempts, 20) - 1), BACKOFF_MAX_S)


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
        self._hold_until: datetime | None = None  # chat-wide pause (429, outage, config)
        self._last_send: datetime | None = None
        self._recovered = False
        self._hold_version: int | None = None  # settings' hold_version last seen

    async def run(self) -> None:
        """Sends while holding the advisory lock (single sender). The lock lives on a
        dedicated connection, checked before every tick: if that connection is lost (and
        the lock with it) nothing is sent until it is retaken."""
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
                                if held:
                                    self._recovered = False  # re-read the hold
                                    warned = False
                                elif not warned:
                                    log.warning("telegram sender: another worker holds the "
                                                "lock; waiting")
                                    warned = True
                            else:
                                await conn.scalar(text("select 1"))  # still holding it
                                await conn.commit()
                            if not held or not await self._tick_logged():
                                await self._sleep(self._interval_s)
                    finally:
                        # never return a connection that may hold the lock to the pool
                        with contextlib.suppress(Exception):
                            await conn.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram sender: lock connection lost; reconnecting")
                await self._sleep(self._interval_s)

    async def _tick_logged(self) -> int:
        try:
            return await self.tick()
        except Exception:
            log.exception("telegram sender tick failed")
            return 0

    async def tick(self) -> int:
        """Sends (or attempts) at most one message; returns 1 when a row was handled."""
        if not self._recovered:
            await self._recover_hold()
        if (self._hold_until is not None and self._clock() < self._hold_until
                and not await self._hold_released()):
            return 0
        if self._last_send is not None:
            wait = (self._last_send + timedelta(seconds=SEND_MIN_INTERVAL_S)
                    - self._clock()).total_seconds()
            if wait > 0:
                await self._sleep(wait)
        async with self._sm() as session, session.begin():
            cfg = await load_config(session)
            self._hold_version = cfg.hold_version
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

    async def _hold_released(self) -> bool:
        """True (and the hold dropped) when the settings' hold_version changed since the
        last tick: the configuration was fixed, the queue resumed or the failed retried."""
        async with self._sm() as session:
            version = await load_hold_version(session)
        seen, self._hold_version = self._hold_version, version
        if seen is None or version == seen:
            return False
        self._hold_until = None
        return True

    async def _recover_hold(self) -> None:
        async with self._sm() as session:
            self._hold_version = await load_hold_version(session)
            head = await session.scalar(
                select(TelegramOutbox)
                .where(TelegramOutbox.status == OutboxStatus.pending)
                .order_by(TelegramOutbox.id)
                .limit(1)
            )
        self._recovered = True
        if (head is not None and head.next_attempt_at > self._clock()
                and kind_of_message(head.last_error) != MESSAGE):
            self._hold_until = head.next_attempt_at

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
                break
            except TelegramError as e:
                if e.migrate_to_chat_id is None or migrated:
                    await self._failed(session, row, e, now)
                    return
                migrated = True
                chat_id = await self._migrate(session, chat_id, str(e.migrate_to_chat_id))
        row.status = OutboxStatus.sent
        row.sent_at = now
        row.telegram_message_id = message_id
        row.last_error = None
        self._hold_until = None

    async def _migrate(self, session: AsyncSession, old: str, new: str) -> str:
        """The group became a supergroup: the configuration takes the new chat id. The row
        is locked first, so a pause or a config saved meanwhile is kept."""
        await lock_settings(session)
        fresh = await load_config(session)
        if fresh.chat_id == old and old != new:
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
            retry_at = now + timedelta(seconds=wait)
        else:
            row.attempts += 1
            retry_at = now + timedelta(seconds=backoff_s(row.attempts))
            if e.kind != TRANSIENT and row.attempts >= MAX_ATTEMPTS:
                row.status = OutboxStatus.failed
                log.warning("telegram message %s failed after %s attempts", row.id,
                            row.attempts)
        if (e.kind in (TRANSIENT, CHAT) and self._hold_version is not None
                and await load_hold_version(session) != self._hold_version):
            # released (config fixed, queue resumed, failed retried) while this send was in
            # flight: keep the release, do not push anything back
            if row.status == OutboxStatus.pending:
                row.next_attempt_at = now
            return
        if row.status == OutboxStatus.pending:
            row.next_attempt_at = retry_at
        if e.kind in (TRANSIENT, CHAT):
            self._hold_until = retry_at
            await session.execute(
                update(TelegramOutbox)
                .where(TelegramOutbox.status == OutboxStatus.pending,
                       TelegramOutbox.id != row.id)
                .values(next_attempt_at=func.greatest(TelegramOutbox.next_attempt_at,
                                                      retry_at))
            )
