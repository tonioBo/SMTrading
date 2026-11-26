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
    false,
    func,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
        CheckConstraint("day_close_minutes BETWEEN 2 AND 60",
                        name="ck_portfolios_day_close_minutes"),
        CheckConstraint("max_day_trades_5d IS NULL OR max_day_trades_5d >= 0",
                        name="ck_portfolios_max_day_trades_5d"),
        CheckConstraint("intraday_max_wakes_per_hour BETWEEN 1 AND 60",
                        name="ck_portfolios_intraday_max_wakes_per_hour"),
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
    # intraday, mode A (agent wake on a signal) and mode B (automatic rules): two independent
    # switches, both off by default (intraday.models.day_scope is true if either is on)
    intraday_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false())
    auto_rules_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false())
    intraday_timeframes: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["15m", "1h"],
        server_default=text("'[\"15m\",\"1h\"]'::jsonb"))
    # the agent woken on a signal (mode A); cleared if the agent is deleted
    intraday_wake_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"))
    # minutes before the close at which the safety net closes the day's quantity (both modes)
    day_close_minutes: Mapped[int] = mapped_column(
        Integer, default=10, server_default=text("10"))
    # optional own day-trade cap in 5 rolling sessions (None: no cap)
    max_day_trades_5d: Mapped[int | None] = mapped_column(Integer)
    intraday_max_wakes_per_hour: Mapped[int] = mapped_column(
        Integer, default=6, server_default=text("6"))
    # mode B: the portfolio's brakes over all its rules (autorules.models.validate_brakes,
    # level "portfolio"; {}: the defaults)
    auto_brakes: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"))


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
