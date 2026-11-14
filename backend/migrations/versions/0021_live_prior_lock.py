"""R8.13: the lock a live anomaly found on a portfolio (status + reason before the live
reason replaced it), restored by the TOTP resume

Revision ID: 0021
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portfolios", sa.Column("live_prior_status", sa.String(16)))
    op.add_column("portfolios", sa.Column("live_prior_reason", sa.String(64)))


def downgrade() -> None:
    op.drop_column("portfolios", "live_prior_reason")
    op.drop_column("portfolios", "live_prior_status")
