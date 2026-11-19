"""The campaigns (migration 0037): a named test run grouping portfolios over a period.

- `status` bozza (prepared, not started) | attiva | conclusa (closed by the close wizard:
  `closed_at`, `closed_by`).
- `campaign_portfolios`: its portfolios; `created` true when the wizard created them.
- `campaign_goals`: the goals the wizard created for it (usually a custom goal per
  portfolio ending at the campaign's end).
Campaigns are admin-only data: no MCP tool shows them (agents see a campaign's end through
its goals)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

STATUSES = ("bozza", "attiva", "conclusa")
MAX_NAME_LEN = 128
MAX_NOTES_LEN = 2000


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("status in ('bozza', 'attiva', 'conclusa')",
                        name="ck_campaigns_status"),
        CheckConstraint("end_date >= start_date", name="ck_campaigns_dates"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LEN), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(MAX_NOTES_LEN))
    status: Mapped[str] = mapped_column(String(16), default="attiva")
    created_by: Mapped[str | None] = mapped_column(String(128))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CampaignPortfolio(Base):
    __tablename__ = "campaign_portfolios"
    __table_args__ = (Index("ix_campaign_portfolios_portfolio", "portfolio_id"),)

    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id"),
                                                   primary_key=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"),
                                                    primary_key=True)
    created: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class CampaignGoal(Base):
    __tablename__ = "campaign_goals"

    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id"),
                                                   primary_key=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id"), primary_key=True)
