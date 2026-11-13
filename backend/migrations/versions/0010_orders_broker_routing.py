"""orders: broker_link_id index, broker_attempt (resubmission counter)

Revision ID: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_orders_broker_link_id", "orders", ["broker_link_id"])
    op.add_column(
        "orders",
        sa.Column("broker_attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("orders", "broker_attempt")
    op.drop_index("ix_orders_broker_link_id", table_name="orders")
