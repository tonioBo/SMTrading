from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base
from smtrading.instruments.models import AssetClass
from smtrading.ledger.models import str_enum
from smtrading.marketdata.types import Capability


class Connector(Base):
    __tablename__ = "connectors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown")  # ok|error|unknown
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProviderPriority(Base):
    __tablename__ = "provider_priorities"

    asset_class: Mapped[AssetClass] = mapped_column(str_enum(AssetClass), primary_key=True)
    capability: Mapped[Capability] = mapped_column(str_enum(Capability), primary_key=True)
    providers: Mapped[list[str]] = mapped_column(JSONB)
