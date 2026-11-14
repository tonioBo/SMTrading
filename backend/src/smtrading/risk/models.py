import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class RiskProfile(Base):
    __tablename__ = "risk_profiles"
    __table_args__ = (
        CheckConstraint(
            "max_live_orders_per_hour BETWEEN 1 AND 1000",
            name="ck_risk_profiles_max_live_orders_per_hour",
        ),
    )

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
    # R8.1 fuse: real orders sent per portfolio in a sliding hour
    max_live_orders_per_hour: Mapped[int] = mapped_column(
        Integer, default=30, server_default=text("30")
    )


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


class HaltScope(enum.StrEnum):
    global_ = "global"
    portfolio = "portfolio"


class TradingHalt(Base):
    """«Blocco aperture»: while active, orders that would increase exposure in the covered
    portfolios (every one for `global`) are refused (TRADING_HALTED). Rows are never
    deleted: clearing one sets `active` false with who and when (the history).

    `set_by` / `cleared_by`: "agent:<uuid>" or "admin:<email>" (the CLI: "admin:cli")."""

    __tablename__ = "trading_halts"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'portfolio')", name="ck_trading_halts_scope"),
        CheckConstraint(
            "(scope = 'global') = (portfolio_id IS NULL)", name="ck_trading_halts_portfolio"
        ),
        CheckConstraint("active = (cleared_at IS NULL)", name="ck_trading_halts_cleared"),
        Index(
            "uq_trading_halts_active_global", "scope", unique=True,
            postgresql_where=text("active AND scope = 'global'"),
        ),
        Index(
            "uq_trading_halts_active_portfolio", "portfolio_id", unique=True,
            postgresql_where=text("active AND scope = 'portfolio'"),
        ),
        Index("ix_trading_halts_set_at", "set_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    reason: Mapped[str] = mapped_column(String(500))
    set_by: Mapped[str] = mapped_column(String(200))
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cleared_by: Mapped[str | None] = mapped_column(String(200))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
