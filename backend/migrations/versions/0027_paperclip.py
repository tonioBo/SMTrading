"""Paperclip reader: the daily cost of each Paperclip agent (USD), per local day

Revision ID: 0027
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_cost_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("paperclip_agent_id", sa.String(64), primary_key=True),
        sa.Column("paperclip_agent_name", sa.String(128)),
        sa.Column("api_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("subscription_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("other_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_cost_days")
