from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

EXPORT_STATUSES = ("confirmed", "rejected", "failed")
_STATUSES = ",".join(f"'{s}'" for s in EXPORT_STATUSES)


class NeurographExport(Base):
    """The last send of one file (type, Rome day, version) to Neurograph's ingest API
    (smtrading.neurograph.sender): `confirmed` (201/200, never sent again), `rejected` (400
    or too big: that version is not retried) or `failed` (401, 5xx, timeout: retried the
    next night). Also the «Fiume dei flussi» export events (flow.events)."""

    __tablename__ = "neurograph_exports"
    __table_args__ = (
        CheckConstraint(f"status in ({_STATUSES})", name="ck_neurograph_exports_status"),
        CheckConstraint("kind in ('decisioni', 'ordini', 'esecuzioni')",
                        name="ck_neurograph_exports_kind"),
        Index("ix_neurograph_exports_sent_at", "sent_at"),
    )

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    rows: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
