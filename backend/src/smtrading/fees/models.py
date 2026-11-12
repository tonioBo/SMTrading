import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class FeeScope(enum.StrEnum):
    global_ = "global"
    asset_class = "asset_class"
    portfolio = "portfolio"


class FeeSide(enum.StrEnum):
    buy = "buy"
    sell = "sell"
    both = "both"


class FeeRule(Base):
    __tablename__ = "fee_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    scope: Mapped[FeeScope] = mapped_column(str_enum(FeeScope))
    scope_ref: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[FeeSide] = mapped_column(str_enum(FeeSide), default=FeeSide.both)
    currency: Mapped[str] = mapped_column(String(16), default="EUR")
    fixed: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    pct: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    min_fee: Mapped[Decimal | None] = mapped_column(NUM)
    max_fee: Mapped[Decimal | None] = mapped_column(NUM)
    tiers: Mapped[list | None] = mapped_column(JSONB)
    fx_pct: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
