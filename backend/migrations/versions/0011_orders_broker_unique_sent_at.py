"""orders: unique broker order per link, broker_sent_at (send attempted)

Revision ID: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("broker_sent_at", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_orders_broker_link_order", "orders", ["broker_link_id", "broker_order_id"],
        unique=True, postgresql_where=sa.text("broker_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_orders_broker_link_order", table_name="orders")
    op.drop_column("orders", "broker_sent_at")
