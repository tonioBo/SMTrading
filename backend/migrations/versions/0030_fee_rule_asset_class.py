"""Fee rules per asset class inside a portfolio (plan 13 Task E): the nullable
`fee_rules.asset_class` (portfolio scope only; null = every class)

Revision ID: 0030
"""
import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fee_rules", sa.Column("asset_class", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("fee_rules", "asset_class")
