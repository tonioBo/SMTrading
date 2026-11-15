from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

USD = Numeric(20, 8)


class AgentCostDay(Base):
    """Cost of one Paperclip agent on one local day (USD), refreshed by the reader."""

    __tablename__ = "agent_cost_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    paperclip_agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paperclip_agent_name: Mapped[str | None] = mapped_column(String(128))
    api_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    subscription_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    other_usd: Mapped[Decimal] = mapped_column(USD, default=Decimal(0))
    runs: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
