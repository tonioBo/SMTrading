"""Plan 29: `orders.synthetic`, an order of a portfolio linked to a broker that takes only
market orders (eToro), kept and watched by SMTrading (limit entries, stops, targets, OCO
legs) and sent to the broker as a market order when its trigger is reached.

Downgrade: the column is dropped. Older code then sees these orders as simulated orders of
a linked portfolio (like a trailing stop): on trigger they are converted to market orders
and sent; a bracket exit activated later would be routed to the broker and refused.

Revision ID: 0042
"""
import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("orders", "synthetic")
