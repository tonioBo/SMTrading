import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class AccountKind(enum.StrEnum):
    cash = "cash"
    position = "position"
    collateral = "collateral"
    margin_loan = "margin_loan"
    system_equity = "system_equity"
    system_fees = "system_fees"
    system_fx = "system_fx"
    system_interest = "system_interest"


class JournalType(enum.StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRADE = "TRADE"
    FEE = "FEE"
    FX = "FX"
    INTEREST = "INTEREST"
    DIVIDEND = "DIVIDEND"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    LIQUIDATION = "LIQUIDATION"
    RESET = "RESET"
    REVERSAL = "REVERSAL"


def str_enum(cls: type[enum.StrEnum]) -> Enum:
    """Store a StrEnum as plain VARCHAR (no native PG enum, no CHECK constraint)."""
    return Enum(
        cls,
        native_enum=False,
        length=32,
        create_constraint=False,
        values_callable=lambda e: [m.value for m in e],
    )


NUM = Numeric(38, 18)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "asset", "kind", name="uq_accounts_portfolio_asset_kind",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    asset: Mapped[str] = mapped_column(String(32))
    kind: Mapped[AccountKind] = mapped_column(str_enum(AccountKind))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Journal(Base):
    __tablename__ = "journals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    type: Mapped[JournalType] = mapped_column(str_enum(JournalType))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    fill_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reversed_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journals.id"), unique=True
    )
    author_type: Mapped[str] = mapped_column(String(16))
    author_id: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    prev_hash: Mapped[str] = mapped_column(CHAR(64))
    hash: Mapped[str] = mapped_column(CHAR(64), unique=True)


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journals.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(NUM)
    unit_price: Mapped[Decimal | None] = mapped_column(NUM)
    price_ccy: Mapped[str | None] = mapped_column(String(16))


class AccountBalance(Base):
    __tablename__ = "account_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(NUM, default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
