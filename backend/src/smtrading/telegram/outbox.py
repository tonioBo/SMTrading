"""The Telegram outbox: rows are written in the caller's transaction and sent by the
worker's OutboxSender."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.telegram.config import TelegramConfig, load_config
from smtrading.telegram.models import OutboxStatus, TelegramOutbox
from smtrading.telegram.render import MAX_LEN, utf16_len

AGENT_WINDOW_S = 60
STALE_AFTER = timedelta(hours=24)  # R7.1: older non-critical news is not sent any more
STALE_MESSAGE = "Non inviato: in pausa da più di 24 ore"
LIVE_CATEGORY = "live"  # real-money messages: never stale-skipped (Ruling R8.4)


async def enqueue(session: AsyncSession, *, source: str, category: str, level: str,
                  text: str, entities: list | None = None,
                  cfg: TelegramConfig | None = None) -> TelegramOutbox:
    """`paused` while the configuration is paused, `pending` otherwise. Over-long texts are
    the caller's bug (split them first with `split_message`); the length is measured in
    UTF-16 units, never less than the character count Telegram allows. `cfg`: the
    configuration already loaded in this transaction (saves a decrypt per message)."""
    if utf16_len(text) > MAX_LEN:
        raise ValueError(f"telegram text longer than {MAX_LEN} UTF-16 units")
    if cfg is None:
        cfg = await load_config(session)
    row = TelegramOutbox(
        source=source, category=category, level=level, text=text, entities=entities or None,
        status=OutboxStatus.paused if cfg.paused else OutboxStatus.pending,
    )
    session.add(row)
    await session.flush()
    return row


async def agent_sent_last_minute(session: AsyncSession, agent_id: uuid.UUID) -> int:
    """Messages the agent queued in the last 60 s. Takes a per-agent transaction lock first,
    so concurrent `send_telegram` calls of one agent count and insert one after the other."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                          {"k": f"tg:{agent_id}"})
    return await session.scalar(
        select(func.count()).select_from(TelegramOutbox).where(
            TelegramOutbox.source == f"agent:{agent_id}",
            TelegramOutbox.created_at > func.now() - text(f"interval '{AGENT_WINDOW_S} seconds'"),
        )
    ) or 0


async def resume_paused(session: AsyncSession) -> int:
    """The paused rows go back to pending, due now. The non-critical ones created more than
    STALE_AFTER ago are `skipped` instead (the planner's rule for old news); critical and
    real-money (category `live`) rows are always sent. Returns the rows resumed."""
    await session.execute(
        update(TelegramOutbox)
        .where(TelegramOutbox.status == OutboxStatus.paused,
               TelegramOutbox.level != "critical",
               TelegramOutbox.category != LIVE_CATEGORY,
               TelegramOutbox.created_at < func.now() - STALE_AFTER)
        .values(status=OutboxStatus.skipped, last_error=STALE_MESSAGE)
    )
    result = await session.execute(
        update(TelegramOutbox)
        .where(TelegramOutbox.status == OutboxStatus.paused)
        .values(status=OutboxStatus.pending, next_attempt_at=func.now())
    )
    return result.rowcount or 0


async def retry_outbox(session: AsyncSession, outbox_id: int) -> TelegramOutbox:
    row = await session.get(TelegramOutbox, outbox_id, with_for_update=True)
    if row is None:
        raise SMTError(ErrorCode.NOT_FOUND, "messaggio non trovato", {"field": "id"})
    if row.status != OutboxStatus.failed:
        raise SMTError(ErrorCode.INVALID_ORDER, "si possono riprovare solo i messaggi non inviati",
                       {"field": "status"})
    row.status = OutboxStatus.pending
    row.attempts = 0
    row.last_error = None
    row.next_attempt_at = await session.scalar(select(func.now()))
    await session.flush()
    return row
