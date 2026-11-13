import enum
from decimal import Decimal

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.ledger.models import NUM, str_enum


class AssetClass(enum.StrEnum):
    us_equity = "us_equity"
    eu_equity = "eu_equity"
    crypto = "crypto"
    forex = "forex"


class InstrumentSource(enum.StrEnum):
    catalog = "catalog"  # the curated catalog (seed)
    manual = "manual"  # created by the admin
    auto = "auto"  # added on demand for an agent (instruments.discovery)


class Instrument(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    asset_class: Mapped[AssetClass] = mapped_column(str_enum(AssetClass))
    market: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(16))
    lot_size: Mapped[Decimal] = mapped_column(NUM, default=Decimal(1))
    tick_size: Mapped[Decimal] = mapped_column(NUM, default=Decimal("0.01"))
    shortable: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_symbols: Mapped[dict] = mapped_column(JSONB, default=dict)
    source: Mapped[InstrumentSource] = mapped_column(
        str_enum(InstrumentSource), default=InstrumentSource.manual, server_default="manual"
    )
    added_by: Mapped[str | None] = mapped_column(String(128))
