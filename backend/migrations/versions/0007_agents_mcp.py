"""agents, agent_portfolios, mcp_audit_log

Revision ID: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(8), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "agent_portfolios",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "portfolio_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("permission", sa.String(8), nullable=False),
        sa.CheckConstraint("permission in ('read','trade')", name="ck_agent_portfolios_permission"),
    )
    op.create_table(
        "mcp_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
        ),
        sa.Column("method", sa.String(128), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mcp_audit_log_agent_created", "mcp_audit_log", ["agent_id", "created_at"])


def downgrade() -> None:
    op.drop_table("mcp_audit_log")
    op.drop_table("agent_portfolios")
    op.drop_table("agents")
