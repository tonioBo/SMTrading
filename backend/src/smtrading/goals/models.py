"""The `goals` table (migration 0031).

- `horizon` week | month | quarter | year: the calendar period (Monday–Sunday, month,
  quarter, year, local time) containing `start_date`; with `recurring` the period rolls
  over by itself (the current one contains today). `custom`: from `start_date` to
  `end_date` included, never recurring.
- `scope` total (the live portfolios together) | portfolio (`portfolio_id`).
- `kind` min (the change to reach at least) | cap (a ceiling not to exceed).
- `target_pct`: the change in percent (2 decimals at most).
Goals are never deleted: DELETE sets `active` false."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

HORIZONS = ("week", "month", "quarter", "year", "custom")
SCOPES = ("total", "portfolio")
KINDS = ("min", "cap")
MAX_NAME_LEN = 80


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(f"horizon in ({_in(HORIZONS)})", name="ck_goals_horizon"),
        CheckConstraint(f"scope in ({_in(SCOPES)})", name="ck_goals_scope"),
        CheckConstraint(f"kind in ({_in(KINDS)})", name="ck_goals_kind"),
        CheckConstraint("(scope = 'portfolio') = (portfolio_id IS NOT NULL)",
                        name="ck_goals_portfolio"),
        CheckConstraint("(horizon = 'custom') = (end_date IS NOT NULL)", name="ck_goals_end"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_goals_dates"),
        CheckConstraint("NOT (recurring AND horizon = 'custom')", name="ck_goals_recurring"),
        CheckConstraint("target_pct > 0", name="ck_goals_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LEN))
    horizon: Mapped[str] = mapped_column(String(16))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str] = mapped_column(String(16))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    target_pct: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    kind: Mapped[str] = mapped_column(String(8), default="min")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
