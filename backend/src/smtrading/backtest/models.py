import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class BacktestStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Backtest(Base):
    __tablename__ = "backtests"
    __table_args__ = (Index("ix_backtests_status_created", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    strategy: Mapped[str] = mapped_column(String(64))
    symbols: Mapped[list] = mapped_column(JSONB)
    timeframe: Mapped[str] = mapped_column(String(16))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_cash: Mapped[Decimal] = mapped_column(NUM)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    status: Mapped[BacktestStatus] = mapped_column(
        str_enum(BacktestStatus), default=BacktestStatus.queued
    )
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchKind(enum.StrEnum):
    walk_forward = "walk_forward"
    param_sweep = "param_sweep"
    portfolio = "portfolio"


class ResearchRun(Base):
    """A research run on historical bars (walk-forward, parameter sweep, portfolio
    backtest): queued and run by the worker like a backtest, with the same lifecycle
    columns; `spec` holds the request (grid, windows, sleeves, sizing...)."""

    __tablename__ = "research_runs"
    __table_args__ = (
        Index("ix_research_runs_status_created", "status", "created_at"),
        Index("ix_research_runs_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL")
    )
    kind: Mapped[ResearchKind] = mapped_column(str_enum(ResearchKind))
    strategy: Mapped[str | None] = mapped_column(String(64))
    symbols: Mapped[list] = mapped_column(JSONB)
    timeframe: Mapped[str] = mapped_column(String(16))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    initial_cash: Mapped[Decimal] = mapped_column(NUM)
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    status: Mapped[BacktestStatus] = mapped_column(
        str_enum(BacktestStatus), default=BacktestStatus.queued
    )
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
