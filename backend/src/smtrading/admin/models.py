import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base


class AdminUser(Base):
    """A web-app administrator. `email` is stored lowercased."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    # encrypt_json({"secret": base32}) under SMT_MASTER_KEY; set by setup, active once enabled
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
