"""«Registro attività»: user_activity, append-only (no UPDATE, DELETE or TRUNCATE), kept
forever

Revision ID: 0023
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("admin_users.id")),
        sa.Column("email", sa.String(254)),
        sa.Column("role", sa.String(16)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("route", sa.String(200), nullable=False),
        sa.Column("target", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column("status", sa.SmallInteger()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("error_message", sa.String(300)),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(300)),
        sa.Column("summary", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("outcome IN ('ok', 'denied', 'error')",
                           name="ck_user_activity_outcome"),
    )
    op.create_index("ix_user_activity_email", "user_activity", ["email", "id"])
    op.create_index("ix_user_activity_action", "user_activity", ["action", "id"])
    op.create_index("ix_user_activity_ts", "user_activity", ["ts"])
    op.execute("""
        CREATE FUNCTION smt_user_activity_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'USER_ACTIVITY_APPEND_ONLY: % on user_activity is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER user_activity_append_only BEFORE UPDATE OR DELETE ON user_activity "
        "FOR EACH ROW EXECUTE FUNCTION smt_user_activity_append_only()"
    )
    op.execute(
        "CREATE TRIGGER user_activity_no_truncate BEFORE TRUNCATE ON user_activity "
        "FOR EACH STATEMENT EXECUTE FUNCTION smt_user_activity_append_only()"
    )


def downgrade() -> None:
    op.drop_table("user_activity")  # drops its triggers
    op.execute("DROP FUNCTION smt_user_activity_append_only()")
