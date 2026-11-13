import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class PortfolioStatus(enum.StrEnum):
    active = "active"
    reduce_only = "reduce_only"
    locked = "locked"


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    base_currency: Mapped[str] = mapped_column(String(16), default="EUR")
    leverage: Mapped[Decimal] = mapped_column(NUM, default=Decimal(1))
    maintenance_margin_pct: Mapped[Decimal] = mapped_column(NUM, default=Decimal(50))
    warning_margin_pct: Mapped[Decimal] = mapped_column(NUM, default=Decimal(75))
    slippage_bps: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[PortfolioStatus] = mapped_column(
        str_enum(PortfolioStatus), default=PortfolioStatus.active
    )
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Rome")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    risk_profile_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("risk_profiles.id"))
    reduce_only_reason: Mapped[str | None] = mapped_column(String(64))
    reduce_only_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("broker_links.id"))
