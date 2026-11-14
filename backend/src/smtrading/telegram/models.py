import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class OutboxStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    paused = "paused"
    skipped = "skipped"  # paused for more than 24 h, non-critical: old news (never sent)


class TelegramOutbox(Base):
    __tablename__ = "telegram_outbox"
    __table_args__ = (
        Index("ix_telegram_outbox_status_next", "status", "next_attempt_at", "id"),
        Index("ix_telegram_outbox_source_created", "source", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(String(64))  # "system" | "agent:<uuid>"
    category: Mapped[str] = mapped_column(String(16))  # event|digest|daily|weekly|agent|test|live
    level: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default=OutboxStatus.pending)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(500))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
