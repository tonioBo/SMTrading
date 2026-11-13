"""orders: partial indexes on open orders (expires_at, created_at)

Revision ID: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_OPEN_WHERE = sa.text("status IN ('new', 'held', 'queued', 'pending', 'triggered')")


def upgrade() -> None:
    op.create_index(
        "ix_orders_open_expires_at", "orders", ["expires_at"], postgresql_where=_OPEN_WHERE
    )
    op.create_index(
        "ix_orders_open_created_at", "orders", ["created_at"], postgresql_where=_OPEN_WHERE
    )


def downgrade() -> None:
    op.drop_index("ix_orders_open_created_at", table_name="orders")
    op.drop_index("ix_orders_open_expires_at", table_name="orders")
