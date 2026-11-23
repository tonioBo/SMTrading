import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    func,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.ledger.models import NUM, str_enum


class PortfolioStatus(enum.StrEnum):
    active = "active"
    reduce_only = "reduce_only"
    locked = "locked"
    # hidden and frozen: no trading, no jobs; the ledger stays as it is (Task 13)
    archived = "archived"


ARCHIVED_MESSAGE = "Portafoglio archiviato"
# the short code the agents use for a portfolio (task titles, messages)
CODE_PATTERN = r"^[A-Z0-9]{2,6}$"


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint(f"code ~ '{CODE_PATTERN}'", name="ck_portfolios_code"),
        # unique among the portfolios that are not archived (an archived one keeps its code)
        Index("uq_portfolios_code_live", "code", unique=True,
              postgresql_where=text("status <> 'archived' AND code IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # optional short code for the agents (2-6 uppercase letters/digits), e.g. «PRU»
    code: Mapped[str | None] = mapped_column(String(6))
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(128))
    # the status to restore on unarchive (active, reduce_only or locked)
    archived_prior_status: Mapped[str | None] = mapped_column(String(32))
    # R8.13: a locked portfolio a live anomaly was recorded on keeps its lock here (status
    # "locked" and the reason the live one replaced); the TOTP resume restores it
    live_prior_status: Mapped[str | None] = mapped_column(String(16))
    live_prior_reason: Mapped[str | None] = mapped_column(String(64))
    # false: left out of the total (the goals on the total, the dashboard totals); its own
    # goals work as usual (a time-boxed experiment beside the team's portfolios)
    in_totals: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


def live_portfolio_ids() -> Select:
    """The ids of the portfolios that are not archived (for `.in_()` filters)."""
    return select(Portfolio.id).where(Portfolio.status != PortfolioStatus.archived)


def total_portfolio_ids() -> Select:
    """The ids of the portfolios counted in the total: not archived and `in_totals`."""
    return live_portfolio_ids().where(Portfolio.in_totals.is_(True))


def ensure_not_archived(portfolio: Portfolio) -> Portfolio:
    """PORTFOLIO_ARCHIVED for an archived portfolio: it is read-only."""
    if portfolio.status == PortfolioStatus.archived:
        raise SMTError(
            ErrorCode.PORTFOLIO_ARCHIVED, ARCHIVED_MESSAGE, {"portfolio_id": str(portfolio.id)}
        )
    return portfolio
