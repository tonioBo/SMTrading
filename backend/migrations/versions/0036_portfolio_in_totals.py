"""Portfolios: `portfolios.in_totals` (default true). A portfolio with in_totals false is
left out of the total: the goals on the total (progress, the piano that covers every
portfolio), the weekly goal surfaces and the dashboard totals; its own goals work as usual.

Downgrade: the column is dropped (every portfolio counts again).

Revision ID: 0036
"""
import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portfolios", sa.Column("in_totals", sa.Boolean(), nullable=False,
                                          server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("portfolios", "in_totals")
