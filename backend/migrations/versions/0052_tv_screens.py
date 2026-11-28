"""«Schermo TV», per user: `tv_screens` (a user's TVs: address, name, encrypted pairing
token, pairing state, last use, revocation), `tv_displays` (per user: the display link,
its secret encrypted, the current selection and the sound settings), `tv_events` (what was sent to a TV:
the «Fiume dei flussi» events Database → Schermo TV).

Downgrade: the tables are dropped (TVs must be paired again).

Revision ID: 0052
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tv_screens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("host", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("mac", sa.String(32), nullable=True),
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("paired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pairing_state", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("pairing_message", sa.String(300), nullable=True),
        sa.Column("pairing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_message", sa.String(300), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "host", name="uq_tv_screens_user_host"),
        sa.CheckConstraint(
            "pairing_state in ('idle','waiting','ok','refused','timeout','error')",
            name="ck_tv_screens_pairing_state"),
    )
    op.create_index("ix_tv_screens_user_id", "tv_screens", ["user_id"])
    op.create_table(
        "tv_displays",
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("link_id", sa.String(32), nullable=True),
        sa.Column("link_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("link_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("screen", sa.String(16), nullable=False, server_default="campaign"),
        sa.Column("rotation", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("interval_s", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("portal_url", sa.String(200), nullable=True),
        sa.Column("sound_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sound_volume", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("quiet_start", sa.String(5), nullable=True, server_default="22:00"),
        sa.Column("quiet_end", sa.String(5), nullable=True, server_default="08:00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("screen in ('flow','campaign','health')",
                           name="ck_tv_displays_screen"),
        sa.CheckConstraint("interval_s between 15 and 300", name="ck_tv_displays_interval"),
        sa.CheckConstraint("sound_volume between 0 and 100", name="ck_tv_displays_volume"),
        sa.UniqueConstraint("link_id", name="uq_tv_displays_link_id"),
    )
    op.create_table(
        "tv_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tv_id", UUID(as_uuid=True),
                  sa.ForeignKey("tv_screens.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("screen", sa.String(16), nullable=True),
        sa.Column("rotation", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("kind in ('send','display','power_off')", name="ck_tv_events_kind"),
    )
    op.create_index("ix_tv_events_created_at", "tv_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tv_events_created_at", table_name="tv_events")
    op.drop_table("tv_events")
    op.drop_table("tv_displays")
    op.drop_index("ix_tv_screens_user_id", table_name="tv_screens")
    op.drop_table("tv_screens")
