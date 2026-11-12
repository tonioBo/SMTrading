import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
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
    filled = "filled"
    canceled = "canceled"
    rejected = "rejected"
    expired = "expired"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "client_order_id", name="uq_orders_portfolio_client"),
        CheckConstraint(
            "(qty IS NULL) <> (notional IS NULL)", name="ck_orders_qty_xor_notional"
        ),
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_portfolio_status", "portfolio_id", "status"),
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
    reject_code: Mapped[str | None] = mapped_column(String(64))
    reject_message: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), default="agent")  # agent | admin | system
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
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
