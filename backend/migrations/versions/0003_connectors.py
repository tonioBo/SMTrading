"""connectors and provider priorities

Revision ID: 0003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("rate_limit_per_min", sa.Integer(), nullable=True),
        sa.Column("health_status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "provider_priorities",
        sa.Column("asset_class", sa.String(32), primary_key=True),
        sa.Column("capability", sa.String(32), primary_key=True),
        sa.Column("providers", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_priorities")
    op.drop_table("connectors")
