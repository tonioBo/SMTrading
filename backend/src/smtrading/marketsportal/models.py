"""Tables of «Mercati v2» (plan 30, migration 0043). Portal only: the agents never read
them (no MCP tool, no resource), and they are apart from the agents' `watchlists` and
`alerts`."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM

ALERT_ACTIVE, ALERT_TRIGGERED = "active", "triggered"
ABOVE, BELOW = "above", "below"
SOURCE_CATALOG, SOURCE_OVERVIEW = "catalog", "overview"


class PortalFavorite(Base):
    """A portal user's starred instrument («I miei preferiti»); `position` orders them."""

    __tablename__ = "portal_favorites"

    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class PortalPriceAlert(Base):
    """The owner's personal price alert: «sopra/sotto X» on a catalog instrument
    (`source` catalog) or a «Mercati oggi» symbol (`source` overview). Sent once to the
    owner's Telegram group, then `triggered`."""

    __tablename__ = "portal_price_alerts"
    __table_args__ = (
        CheckConstraint("direction in ('above','below')", name="ck_portal_price_alerts_direction"),
        CheckConstraint("status in ('active','triggered')", name="ck_portal_price_alerts_status"),
        CheckConstraint("source in ('catalog','overview')", name="ck_portal_price_alerts_source"),
        Index("ix_portal_price_alerts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))
    threshold: Mapped[Decimal] = mapped_column(NUM)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default=ALERT_ACTIVE,
                                        server_default=ALERT_ACTIVE)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_price: Mapped[Decimal | None] = mapped_column(NUM)


class MarketScreenRow(Base):
    """The technical figures of one catalog instrument from its daily bars (the
    screener and the multi-period sector map), precomputed by the worker."""

    __tablename__ = "market_screen_rows"

    symbol: Mapped[str] = mapped_column(
        String(64), ForeignKey("instruments.symbol", ondelete="CASCADE"), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date)  # the session date of the last daily bar
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bars: Mapped[int] = mapped_column(Integer)
    close: Mapped[Decimal] = mapped_column(NUM)
    rsi14: Mapped[Decimal | None] = mapped_column(NUM)
    sma200: Mapped[Decimal | None] = mapped_column(NUM)
    high_52w: Mapped[Decimal | None] = mapped_column(NUM)
    low_52w: Mapped[Decimal | None] = mapped_column(NUM)
    day_high: Mapped[Decimal | None] = mapped_column(NUM)
    day_low: Mapped[Decimal | None] = mapped_column(NUM)
    volume: Mapped[Decimal | None] = mapped_column(NUM)
    avg_volume20: Mapped[Decimal | None] = mapped_column(NUM)
    chg_1d: Mapped[Decimal | None] = mapped_column(NUM)
    chg_1w: Mapped[Decimal | None] = mapped_column(NUM)
    chg_1m: Mapped[Decimal | None] = mapped_column(NUM)
    chg_ytd: Mapped[Decimal | None] = mapped_column(NUM)
