import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(8), unique=True)
    token_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentPortfolio(Base):
    __tablename__ = "agent_portfolios"
    __table_args__ = (
        CheckConstraint("permission in ('read','trade')", name="ck_agent_portfolios_permission"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    permission: Mapped[str] = mapped_column(String(8))


class McpAuditLog(Base):
    __tablename__ = "mcp_audit_log"
    __table_args__ = (Index("ix_mcp_audit_log_agent_created", "agent_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    method: Mapped[str] = mapped_column(String(128))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
