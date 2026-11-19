import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

CHECK_STATUSES = ("ok", "warning", "mismatch", "unverifiable")
_STATUSES = ",".join(f"'{s}'" for s in CHECK_STATUSES)


class ReconciliationCheck(Base):
    """One check of a broker-linked portfolio against its broker account (plan 21,
    smtrading.reconciliation.service): what SMTrading and the broker say, item by item."""

    __tablename__ = "reconciliation_checks"
    __table_args__ = (
        CheckConstraint(f"status in ({_STATUSES})", name="ck_reconciliation_checks_status"),
        CheckConstraint("trigger in ('schedule', 'manual')",
                        name="ck_reconciliation_checks_trigger"),
        Index("ix_reconciliation_checks_portfolio_ts", "portfolio_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"))
    broker_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_links.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16))
    # [{key, kind, label, symbol, ours, broker, diff, status, note, streak, since, alerted}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # {currency, orders_checked, link_portfolios}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(String(500))
