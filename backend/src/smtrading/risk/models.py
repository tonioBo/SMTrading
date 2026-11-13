import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class RiskProfile(Base):
    __tablename__ = "risk_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    max_order_notional_eur: Mapped[Decimal | None] = mapped_column(NUM)
    max_position_pct: Mapped[Decimal | None] = mapped_column(NUM)
    max_daily_loss_pct: Mapped[Decimal | None] = mapped_column(NUM)
    max_daily_loss_eur: Mapped[Decimal | None] = mapped_column(NUM)
    allowed_symbols: Mapped[list[str]] = mapped_column(JSONB, default=list)
    allowed_asset_classes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    allowed_markets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    short_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)


class InterestKind(enum.StrEnum):
    loan = "loan"
    borrow = "borrow"


class InterestRate(Base):
    """`key` is a currency (loan) or an asset class (borrow); "*" is the loan fallback."""

    __tablename__ = "interest_rates"

    kind: Mapped[InterestKind] = mapped_column(str_enum(InterestKind), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    annual_pct: Mapped[Decimal] = mapped_column(NUM)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class SnapshotKind(enum.StrEnum):
    periodic = "periodic"
    day_open = "day_open"


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        # NULL local_date (periodic rows) never collides; day_open rows are unique per date
        UniqueConstraint(
            "portfolio_id", "kind", "local_date", name="uq_equity_snapshots_day_open"
        ),
        Index("ix_equity_snapshots_portfolio_ts", "portfolio_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[SnapshotKind] = mapped_column(str_enum(SnapshotKind))
    local_date: Mapped[date | None] = mapped_column(Date)
    equity_eur: Mapped[Decimal] = mapped_column(NUM)
    cash_eur: Mapped[Decimal] = mapped_column(NUM)
    exposure_eur: Mapped[Decimal] = mapped_column(NUM)
    margin_used_eur: Mapped[Decimal] = mapped_column(NUM)


class JobRun(Base):
    __tablename__ = "job_runs"

    job: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """`payload` is JSONB: callers must store Decimals as strings (use decimal_to_str)."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    agent_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Any] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
