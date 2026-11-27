import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class BrokerLink(Base):
    __tablename__ = "broker_links"
    __table_args__ = (
        CheckConstraint(
            "live_state IS NULL OR live_state IN ('configured', 'dry_run', 'armed')",
            name="ck_broker_links_live_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32))  # alpaca_paper | t212_demo
    config_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok | error | unknown
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # real-money links only (brokers.live.models.LiveState); NULL for paper/demo links
    live_state: Mapped[str | None] = mapped_column(String(16))
    armed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disarmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    armed_by: Mapped[str | None] = mapped_column(String(128))
    disarmed_reason: Mapped[str | None] = mapped_column(String(200))
    account_fingerprint: Mapped[str | None] = mapped_column(String(64))  # sha256 kind:account
    last_account_snapshot: Mapped[dict | None] = mapped_column(JSONB)  # without `raw`
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # paper/demo links (brokers.health): the last outage, from its first unanswered call
    # to the first answer after it (None while it lasts)
    outage_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outage_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # real-money links: {currency: units of the account currency per unit}, the last FX
    # rates at which the linked cash matched the broker's (risk.live_checks, reconcile)
    fx_ref_rates: Mapped[dict | None] = mapped_column(JSONB)
    # intraday: the broker's account state (buying power, multiplier, equity, blocks; the
    # name is from migration 0046, the PDT rule is gone), read by intraday.account_reader;
    # never the account id nor a secret
    pdt_state: Mapped[dict | None] = mapped_column(JSONB)
    pdt_state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    __table_args__ = (  # the previous reconciliation of a portfolio on a link (0017)
        Index("ix_reconciliations_link_portfolio_ts", "broker_link_id", "portfolio_id", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_link_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_links.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    diffs: Mapped[list] = mapped_column(JSONB, default=list)  # [{symbol, ours, broker}]
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
