import enum
import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class LiveState(enum.StrEnum):
    """`BrokerLink.live_state` of a real-money link (NULL for paper/demo links)."""

    configured = "configured"  # keys saved: nothing reaches the broker except GET/DELETE
    dry_run = "dry_run"  # order POSTs are intercepted, the payload is recorded
    armed = "armed"  # real orders are sent


LIVE_AUDIT_ACTIONS = (
    "keys_saved", "test_ok", "test_failed", "dry_run_on", "dry_run_off", "armed", "disarmed",
    "portfolio_linked", "portfolio_unlinked", "reduce_only_auto", "resumed", "kill_switch",
    "flatten", "order_sent",
)


class LiveAudit(Base):
    """Append-only (trigger live_audit_append_only): every step of a real-money link."""

    __tablename__ = "live_audit"
    __table_args__ = (
        CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in LIVE_AUDIT_ACTIONS) + ")",
            name="ck_live_audit_action",
        ),
        Index("ix_live_audit_link_id", "link_id", "id"),
        Index("ix_live_audit_portfolio_id", "portfolio_id", "id"),
    )
    __mapper_args__: ClassVar[dict] = {"eager_defaults": True}  # id and ts come back with the INSERT

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_links.id"))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    actor: Mapped[str] = mapped_column(String(128))  # admin email, "system", "agent:<uuid>"
    action: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
