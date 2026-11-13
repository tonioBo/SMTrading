"""portfolios: archived_at, archived_by, archived_prior_status (status `archived`)

Revision ID: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portfolios", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column("portfolios", sa.Column("archived_by", sa.String(128)))
    op.add_column("portfolios", sa.Column("archived_prior_status", sa.String(32)))


def downgrade() -> None:
    op.execute(
        "UPDATE portfolios SET status = coalesce(archived_prior_status, 'active')"
        " WHERE status = 'archived'"
    )
    op.drop_column("portfolios", "archived_prior_status")
    op.drop_column("portfolios", "archived_by")
    op.drop_column("portfolios", "archived_at")
