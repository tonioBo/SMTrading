import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class AlertCondition(enum.StrEnum):
    price_above = "price_above"
    price_below = "price_below"
    # percent change is measured against Alert.ref_price (the quote at creation time)
    change_pct_above = "change_pct_above"
    change_pct_below = "change_pct_below"


class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_watchlists_agent_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    watchlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_agent_status", "agent_id", "status"),
        Index("ix_alerts_symbol_status", "symbol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(64))
    condition: Mapped[AlertCondition] = mapped_column(str_enum(AlertCondition))
    value: Mapped[Decimal] = mapped_column(NUM)
    ref_price: Mapped[Decimal | None] = mapped_column(NUM)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|triggered|deleted
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_price: Mapped[Decimal | None] = mapped_column(NUM)
    note: Mapped[str | None] = mapped_column(String(512))
