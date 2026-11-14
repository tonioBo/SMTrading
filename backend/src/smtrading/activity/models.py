import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class ActivityLevel(enum.StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"
    executed = "executed"


class TelegramState(enum.StrEnum):
    pending = "pending"
    queued = "queued"
    digest = "digest"
    skipped = "skipped"


class DecisionKind(enum.StrEnum):
    proposal = "proposal"
    approval = "approval"
    veto = "veto"
    allocation = "allocation"
    strategy = "strategy"
    risk_rule = "risk_rule"
    other = "other"


_DECISION_KINDS = ",".join(f"'{k.value}'" for k in DecisionKind)


class Activity(Base):
    __tablename__ = "activity"
    __table_args__ = (
        Index("ix_activity_portfolio_id", "portfolio_id", "id"),
        Index("ix_activity_ts", "ts"),
        Index(
            "ix_activity_telegram_open",
            "id",
            postgresql_where=text("telegram IN ('pending','digest')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    type: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))
    # no foreign key (0016): recording an activity never aborts the business transaction
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id")
    )
    summary: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    telegram: Mapped[str | None] = mapped_column(String(16))
    outbox_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("telegram_outbox.id", ondelete="SET NULL")
    )


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(f"kind in ({_DECISION_KINDS})", name="ck_decisions_kind"),
        Index("ix_decisions_portfolio_id", "portfolio_id", "id"),
        Index("ix_decisions_agent_id", "agent_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id")
    )
    kind: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    symbols: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
