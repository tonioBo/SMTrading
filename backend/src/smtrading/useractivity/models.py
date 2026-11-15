import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

OUTCOMES = ("ok", "denied", "error", "pending")


class UserActivity(Base):
    """Append-only (trigger user_activity_append_only, kept forever): one row per unsafe
    /api request of a person, plus the access events (`auth.*`). `route` is the route
    template, never the raw path; `target` the path parameters; `summary` the request
    body with every secret masked. Rows of the same request share `request_id` (also put
    in the params of the `admin:*` rows of mcp_audit_log).

    `provisional`: the row the request's own commit carries, written before the answer
    exists (outcome `pending`); the final row of the same request_id, written after the
    answer, is the authoritative one (the API shows the final row only)."""

    __tablename__ = "user_activity"
    __table_args__ = (
        CheckConstraint("outcome IN ('ok', 'denied', 'error', 'pending')",
                        name="ck_user_activity_outcome"),
        Index("ix_user_activity_email", "email", "id"),
        Index("ix_user_activity_action", "action", "id"),
        Index("ix_user_activity_category", "category", "id"),
        Index("ix_user_activity_request_id", "request_id"),
        Index("ix_user_activity_ts", "ts"),
    )
    __mapper_args__: ClassVar[dict] = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    email: Mapped[str | None] = mapped_column(String(254))  # also the attempted login email
    role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))  # the action's prefix (filter index)
    method: Mapped[str] = mapped_column(String(8))
    route: Mapped[str] = mapped_column(String(200))
    target: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"))
    outcome: Mapped[str] = mapped_column(String(8))
    status: Mapped[int | None] = mapped_column(SmallInteger)
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"))
    provisional: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
