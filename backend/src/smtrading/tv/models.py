"""«Schermo TV», per user (every portal user has their own area):

    tv_screens   one TV of a user: address, name, model, the pairing token (encrypted:
                 `encrypt_json({"token"}, master key, aad=b"tv-token")`, never returned),
                 the pairing in progress and the last use
    tv_displays  one row per user: the display link (id + secret encrypted with
                 aad=b"tv-link"; null = revoked), the current selection (screen,
                 rotation, interval), the portal address the TV opens and the sound
                 settings (off by default, volume, quiet hours)
    tv_events    what was sent to a TV (a screen, the selection, the power-off): the
                 «Fiume dei flussi» events Database → Schermo TV
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

SCREEN_KEYS = ("flow", "campaign", "health")
PAIRING_STATES = ("idle", "waiting", "ok", "refused", "timeout", "error")
EVENT_KINDS = ("send", "display", "power_off")


class TvScreen(Base):
    __tablename__ = "tv_screens"
    __table_args__ = (
        UniqueConstraint("user_id", "host", name="uq_tv_screens_user_host"),
        CheckConstraint("pairing_state in ('idle','waiting','ok','refused','timeout','error')",
                        name="ck_tv_screens_pairing_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    host: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(64))
    mac: Mapped[str | None] = mapped_column(String(32))
    token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pairing_state: Mapped[str] = mapped_column(String(16), default="idle", server_default="idle")
    pairing_message: Mapped[str | None] = mapped_column(String(300))
    pairing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_message: Mapped[str | None] = mapped_column(String(300))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TvDisplay(Base):
    __tablename__ = "tv_displays"
    __table_args__ = (
        CheckConstraint("screen in ('flow','campaign','health')", name="ck_tv_displays_screen"),
        CheckConstraint("interval_s between 15 and 300", name="ck_tv_displays_interval"),
        CheckConstraint("sound_volume between 0 and 100", name="ck_tv_displays_volume"),
        UniqueConstraint("link_id", name="uq_tv_displays_link_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True)
    link_id: Mapped[str | None] = mapped_column(String(32))
    link_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    link_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    screen: Mapped[str] = mapped_column(String(16), default="campaign", server_default="campaign")
    rotation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    interval_s: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    portal_url: Mapped[str | None] = mapped_column(String(200))
    # sounds on the TV (real events only; off by default; quiet hours in SMT_TZ)
    sound_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    sound_volume: Mapped[int] = mapped_column(Integer, default=40, server_default="40")
    quiet_start: Mapped[str | None] = mapped_column(String(5), default="22:00",
                                                    server_default="22:00")
    quiet_end: Mapped[str | None] = mapped_column(String(5), default="08:00",
                                                  server_default="08:00")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TvEvent(Base):
    __tablename__ = "tv_events"
    __table_args__ = (
        CheckConstraint("kind in ('send','display','power_off')", name="ck_tv_events_kind"),
        Index("ix_tv_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"))
    tv_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tv_screens.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))
    screen: Mapped[str | None] = mapped_column(String(16))
    rotation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ok: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
