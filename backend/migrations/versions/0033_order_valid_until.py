"""Orders, plan 16 Task A: `orders.valid_until` (the good-till-time of an entry) and its
open-orders partial index (the processor's scan of the broker orders to cancel).

Downgrade: the column and the index are dropped. An open order keeps its `expires_at`
(already the earlier of the day expiry and valid_until), so older code still expires it
locally; one already at the broker is then no longer canceled by SMTrading at
valid_until (it stays open at the broker until canceled by hand).

Revision ID: 0033
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_OPEN_WHERE = sa.text(
    "status IN ('new', 'held', 'queued', 'pending', 'triggered', 'partially_filled')"
)


def upgrade() -> None:
    op.add_column("orders", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_orders_open_valid_until", "orders", ["valid_until"],
                    postgresql_where=_OPEN_WHERE)


def downgrade() -> None:
    op.drop_index("ix_orders_open_valid_until", table_name="orders")
    op.drop_column("orders", "valid_until")
