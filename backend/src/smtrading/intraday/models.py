"""Intraday trading (mode A: agent wake on a signal; mode B: automatic rules): the trigger
log and the constants shared by both. Everything is off by default."""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM
from smtrading.portfolios.models import Portfolio

INTRADAY_TIMEFRAMES = ("5m", "15m", "1h")
DEFAULT_TIMEFRAMES = ("15m", "1h")

# the outcome of a trigger (mode A)
PENDING = "pending"
WOKEN = "woken"
TELEGRAM = "telegram"
CAPPED = "capped"
ISSUE_OPEN = "issue_open"
NO_AGENT = "no_agent"
LATE = "late"
FAILED = "failed"
OUTCOMES = (PENDING, WOKEN, TELEGRAM, CAPPED, ISSUE_OPEN, NO_AGENT, LATE, FAILED)

# the SQL twin of day_scope: portfolios with either intraday switch on
DAY_SCOPE = or_(Portfolio.intraday_enabled, Portfolio.auto_rules_enabled)


def day_scope(p: Portfolio) -> bool:
    """True if the portfolio has mode A or mode B on (PDT, close safety net, jobs)."""
    return bool(p.intraday_enabled or p.auto_rules_enabled)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class IntradayTrigger(Base):
    """One signal that fired on an intraday timeframe. Never edited except `outcome`,
    `issue_ident` and `error`, once, from `pending` to the final outcome."""

    __tablename__ = "intraday_triggers"
    __table_args__ = (
        CheckConstraint("rule IN ('entry', 'exit')", name="ck_intraday_triggers_rule"),
        CheckConstraint(f"outcome IN ({_in(OUTCOMES)})", name="ck_intraday_triggers_outcome"),
        Index("ix_intraday_triggers_portfolio_created", "portfolio_id", "created_at"),
        Index("ix_intraday_triggers_portfolio_symbol_id", "portfolio_id", "symbol", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"))
    strategy_ref: Mapped[str] = mapped_column(String(128))
    strategy_name: Mapped[str] = mapped_column(String(200))
    symbol: Mapped[str] = mapped_column(String(32))
    rule: Mapped[str] = mapped_column(String(8))
    timeframe: Mapped[str] = mapped_column(String(8))
    bar_close: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[Decimal | None] = mapped_column(NUM)
    threshold: Mapped[Decimal | None] = mapped_column(NUM)
    price: Mapped[Decimal | None] = mapped_column(NUM)
    rule_text: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(16), default=PENDING)
    issue_ident: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(200))
