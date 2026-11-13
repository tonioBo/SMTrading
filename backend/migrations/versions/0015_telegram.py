"""activity feed, decisions, telegram outbox, agents.can_notify

Revision ID: 0015
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_KINDS = "'proposal','approval','veto','allocation','strategy','risk_rule','other'"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("can_notify", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_table(
        "telegram_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("entities", postgresql.JSONB()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_error", sa.String(500)),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_telegram_outbox_status_next", "telegram_outbox", ["status", "next_attempt_at", "id"]
    )
    op.create_index("ix_telegram_outbox_source_created", "telegram_outbox", ["source", "created_at"])
    op.create_table(
        "activity",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("clock_timestamp()")
        ),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.UUID(), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id")),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("telegram", sa.String(16)),
        sa.Column(
            "outbox_id", sa.BigInteger(), sa.ForeignKey("telegram_outbox.id", ondelete="SET NULL")
        ),
    )
    op.create_index("ix_activity_portfolio_id", "activity", ["portfolio_id", "id"])
    op.create_index(
        "ix_activity_telegram_open",
        "activity",
        ["id"],
        postgresql_where=sa.text("telegram IN ('pending','digest')"),
    )
    op.create_table(
        "decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.UUID(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"kind in ({_KINDS})", name="ck_decisions_kind"),
    )
    op.create_index("ix_decisions_portfolio_id", "decisions", ["portfolio_id", "id"])
    op.create_index("ix_decisions_agent_id", "decisions", ["agent_id", "id"])


def downgrade() -> None:
    op.drop_table("decisions")
    op.drop_table("activity")
    op.drop_table("telegram_outbox")
    op.drop_column("agents", "can_notify")
