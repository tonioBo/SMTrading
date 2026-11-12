import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM


class Position(Base):
    """Projection of the ledger: signed qty, average price, realized P&L (instrument currency)."""

    __tablename__ = "positions"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    realized_pnl: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
