import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"  # «Sola lettura»: reads, plus the protective emergency actions
ROLES = (ROLE_ADMIN, ROLE_VIEWER)


class AdminUser(Base):
    """A portal user (`role` admin or viewer). `email` is stored lowercased. Users are
    never deleted: `disabled_at` deactivates them (no login, sessions refused)."""

    __tablename__ = "admin_users"
    __table_args__ = (CheckConstraint("role in ('admin','viewer')", name="ck_admin_users_role"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    # encrypt_json({"secret": base32}) under SMT_MASTER_KEY; set by setup, active once enabled
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_ADMIN, server_default=ROLE_ADMIN)
    # set with a temporary password: every route but me, logout and the change is refused
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(254))  # creator's email, or "cli"

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def active(self) -> bool:
        return self.disabled_at is None
