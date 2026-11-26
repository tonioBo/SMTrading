"""Mode B tables: the automatic rules, their versions and their trades (migration 0047).

A rule has one symbol and one current `version`; every change of its parameters, size or
brakes writes a new `auto_rule_versions` row (never edited). At most one rule is `active`
per (portfolio, symbol): the partial unique index is the guarantee. `auto_trades` holds
one row per entry trigger, unique per (rule, trigger_key): a trigger is never sent twice."""
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
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM

# rule statuses
DRAFT = "draft"
ACTIVE = "active"
PAUSED = "paused"
BRAKED = "braked"
STATUSES = (DRAFT, ACTIVE, PAUSED, BRAKED)
STATUS_IT = {DRAFT: "bozza", ACTIVE: "attiva", PAUSED: "in pausa",
             BRAKED: "fermata dai freni"}

MODEL_NAMES = ("breakout", "mean_reversion", "vwap_pullback")

# trade statuses
SENT = "sent"
OPEN = "open"
CLOSED = "closed"
CANCELED = "canceled"
REJECTED = "rejected"
TRADE_STATUSES = (SENT, OPEN, CLOSED, CANCELED, REJECTED)

# exit reasons
EXIT_REASONS = ("stop", "target", "time", "session", "manual", "day_close")
# where a fill time comes from
FILL_TS_SOURCES = ("broker", "local")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AutoRule(Base):
    __tablename__ = "auto_rules"
    __table_args__ = (
        CheckConstraint(f"model IN ({_in(MODEL_NAMES)})", name="ck_auto_rules_model"),
        CheckConstraint(f"status IN ({_in(STATUSES)})", name="ck_auto_rules_status"),
        Index("uq_auto_rules_active_symbol", "portfolio_id", "symbol", unique=True,
              postgresql_where=text("status = 'active'")),
        Index("ix_auto_rules_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(32), ForeignKey("instruments.symbol"))
    model: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=DRAFT)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    status_reason: Mapped[str | None] = mapped_column(String(200))
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AutoRuleVersion(Base):
    """One version of a rule's parameters, size and brakes (JSON-safe: decimals as
    strings). Never edited."""

    __tablename__ = "auto_rule_versions"
    __table_args__ = (
        CheckConstraint("(qty IS NULL) <> (notional IS NULL)",
                        name="ck_auto_rule_versions_qty_xor_notional"),
        CheckConstraint("(qty IS NULL OR qty > 0) AND (notional IS NULL OR notional > 0)",
                        name="ck_auto_rule_versions_size_positive"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auto_rules.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    params: Mapped[dict] = mapped_column(JSONB)
    qty: Mapped[Decimal | None] = mapped_column(NUM)
    notional: Mapped[Decimal | None] = mapped_column(NUM)
    brakes: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(64))


class AutoTrade(Base):
    """One entry trigger of a rule and what became of it (entry, exit, result, latency)."""

    __tablename__ = "auto_trades"
    __table_args__ = (
        UniqueConstraint("rule_id", "trigger_key", name="uq_auto_trades_rule_trigger"),
        CheckConstraint(f"status IN ({_in(TRADE_STATUSES)})", name="ck_auto_trades_status"),
        CheckConstraint(f"exit_reason IS NULL OR exit_reason IN ({_in(EXIT_REASONS)})",
                        name="ck_auto_trades_exit_reason"),
        CheckConstraint(
            f"fill_ts_source IS NULL OR fill_ts_source IN ({_in(FILL_TS_SOURCES)})",
            name="ck_auto_trades_fill_ts_source"),
        Index("ix_auto_trades_portfolio_trigger_at", "portfolio_id", "trigger_at"),
        Index("ix_auto_trades_rule_status", "rule_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("auto_rules.id", ondelete="CASCADE"))
    rule_version: Mapped[int] = mapped_column(Integer)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"))
    symbol: Mapped[str] = mapped_column(String(32))
    trigger_key: Mapped[str] = mapped_column(String(64))
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trigger_price: Mapped[Decimal] = mapped_column(NUM)
    trigger_bid: Mapped[Decimal | None] = mapped_column(NUM)
    trigger_ask: Mapped[Decimal | None] = mapped_column(NUM)
    trigger_values: Mapped[dict] = mapped_column(JSONB)
    expected_price: Mapped[Decimal] = mapped_column(NUM)
    status: Mapped[str] = mapped_column(String(16))
    entry_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fill_ts_source: Mapped[str | None] = mapped_column(String(8))
    fill_price: Mapped[Decimal | None] = mapped_column(NUM)
    qty: Mapped[Decimal | None] = mapped_column(NUM)
    exit_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"))
    exit_reason: Mapped[str | None] = mapped_column(String(16))
    exit_trigger_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_expected: Mapped[Decimal | None] = mapped_column(NUM)
    exit_filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[Decimal | None] = mapped_column(NUM)
    fees: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0), server_default=text("0"))
    fees_currency: Mapped[str | None] = mapped_column(String(16))
    net_result: Mapped[Decimal | None] = mapped_column(NUM)
    net_currency: Mapped[str | None] = mapped_column(String(16))
    entry_slippage_bps: Mapped[Decimal | None] = mapped_column(NUM)
    exit_slippage_bps: Mapped[Decimal | None] = mapped_column(NUM)
    ms_trigger_to_send: Mapped[int | None] = mapped_column(Integer)
    ms_send_to_broker: Mapped[int | None] = mapped_column(Integer)
    ms_broker_to_fill: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
