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
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class OrderType(enum.StrEnum):
    market = "market"
    limit = "limit"
    stop = "stop"
    stop_limit = "stop_limit"
    trailing_stop = "trailing_stop"


class OrderSide(enum.StrEnum):
    buy = "buy"
    sell = "sell"


class TimeInForce(enum.StrEnum):
    day = "day"
    gtc = "gtc"


class OrderStatus(enum.StrEnum):
    new = "new"
    held = "held"  # bracket child waiting for its parent
    queued = "queued"  # market closed
    pending = "pending"  # waiting for the trigger
    triggered = "triggered"  # stop fired, became market/limit
    partially_filled = "partially_filled"  # broker fill in progress; still open
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"
    expired = "expired"


# The single source of truth for order status groups.
# fillable: execute_fill may fill it; open: not final yet (fillable or a held bracket child)
FILLABLE_STATUSES = frozenset(
    {
        OrderStatus.new,
        OrderStatus.queued,
        OrderStatus.pending,
        OrderStatus.triggered,
        OrderStatus.partially_filled,
    }
)
OPEN_STATUSES = FILLABLE_STATUSES | {OrderStatus.held}

# partial indexes for the processor's scans of open orders (migrations 0005, 0009)
_OPEN_WHERE = text(
    "status IN ("
    + ", ".join(f"'{s.value}'" for s in OrderStatus if s in OPEN_STATUSES)
    + ")"
)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "client_order_id", name="uq_orders_portfolio_client"),
        CheckConstraint(
            "(qty IS NULL) <> (notional IS NULL)", name="ck_orders_qty_xor_notional"
        ),
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_portfolio_status", "portfolio_id", "status"),
        Index("ix_orders_open_expires_at", "expires_at", postgresql_where=_OPEN_WHERE),
        Index("ix_orders_open_created_at", "created_at", postgresql_where=_OPEN_WHERE),
        Index("ix_orders_open_valid_until", "valid_until", postgresql_where=_OPEN_WHERE),
        Index("ix_orders_broker_link_id", "broker_link_id"),
        # one local order per broker order (C1: never adopt another order's fill)
        Index(
            "uq_orders_broker_link_order", "broker_link_id", "broker_order_id", unique=True,
            postgresql_where=text("broker_order_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"))
    agent_id: Mapped[str | None] = mapped_column(String(128))
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide))
    type: Mapped[OrderType] = mapped_column(str_enum(OrderType))
    tif: Mapped[TimeInForce] = mapped_column(str_enum(TimeInForce), default=TimeInForce.day)
    qty: Mapped[Decimal | None] = mapped_column(NUM)
    notional: Mapped[Decimal | None] = mapped_column(NUM)
    limit_price: Mapped[Decimal | None] = mapped_column(NUM)
    stop_price: Mapped[Decimal | None] = mapped_column(NUM)
    trail_pct: Mapped[Decimal | None] = mapped_column(NUM)
    trail_amount: Mapped[Decimal | None] = mapped_column(NUM)
    trail_ref_price: Mapped[Decimal | None] = mapped_column(NUM)  # hwm (sell) / lwm (buy)
    filled_qty: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(NUM)
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus), default=OrderStatus.new)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    oco_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str | None] = mapped_column(String(16))  # entry | take_profit | stop_loss
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # plan 16: the entry's good-till-time (expires_at is the earlier of it and the day
    # expiry); at the broker the processor cancels it then (engine.service docstring)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_code: Mapped[str | None] = mapped_column(String(64))
    reject_message: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), default="agent")  # agent | admin | system
    # Ruling R22: closes and bracket exits; clamped to |position| at fill, canceled if flat
    reduce_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # R8.2: reduce-only market order of "Chiudi tutte le posizioni reali" (passes the kill switch)
    live_flatten: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # plan 29: kept and watched here on a link that takes only market orders (eToro); at
    # the trigger handed to BrokerSync with its own type, sent as a market order
    synthetic: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    broker_link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("broker_links.id"))
    broker_status: Mapped[str | None] = mapped_column(String(32))
    broker_replicated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    broker_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # resubmissions after a modify (cancel + new order): the broker client reference
    broker_attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # a send of the current attempt was started: the broker may have it (C2)
    broker_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"), index=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide))
    qty: Mapped[Decimal] = mapped_column(NUM)
    price: Mapped[Decimal] = mapped_column(NUM)
    quote_bid: Mapped[Decimal | None] = mapped_column(NUM)
    quote_ask: Mapped[Decimal | None] = mapped_column(NUM)
    quote_last: Mapped[Decimal | None] = mapped_column(NUM)
    quote_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="sim")  # sim | broker
    fee_amount: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    fee_currency: Mapped[str] = mapped_column(String(16))
    fee_rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journals.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


LIQUIDATION_ROLE = "liquidation"  # Order.role of a broker-routed margin liquidation
BROKER_LIQUIDATION_REASON = "broker_liquidation"  # Portfolio.reduce_only_reason while locked


class BrokerState:
    """`Order.broker_status` values set locally (the others are BrokerStatus values).

    Only BrokerSync talks to the broker (no network I/O under the portfolio lock): the
    engine records what it wants and the sync loop carries it out."""

    TO_SEND = "to_send"  # routed, waiting for BrokerSync to send it
    SENDING = "sending"  # claimed by BrokerSync, request in flight
    UNKNOWN = "unknown"  # non-idempotent placement failed after sending (Ruling R7)
    CANCEL_REQUESTED = "cancel_requested"  # still open until the broker confirms
    REPLACE_REQUESTED = "replace_requested"  # modify: cancel, then send again
    PENDING_WORK = frozenset({TO_SEND, SENDING, UNKNOWN, CANCEL_REQUESTED, REPLACE_REQUESTED})
