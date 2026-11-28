"""Richer decisions and the order -> decision link (Neurograph export): optional
`decisions.confidence` (0..100, how sure the agent is) and `decisions.expected`
({return_pct, horizon_days}); `orders.decision_id`, the agent's decision that sent the
order (ON DELETE SET NULL).

Downgrade: the three columns are dropped.

Revision ID: 0049
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("confidence", sa.SmallInteger(), nullable=True))
    op.add_column("decisions", sa.Column("expected", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_decisions_confidence", "decisions",
        "confidence IS NULL OR confidence BETWEEN 0 AND 100",
    )
    op.add_column("orders", sa.Column("decision_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_orders_decision_id", "orders", "decisions",
                          ["decision_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_decision_id", "orders", ["decision_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_decision_id", table_name="orders")
    op.drop_constraint("fk_orders_decision_id", "orders", type_="foreignkey")
    op.drop_column("orders", "decision_id")
    op.drop_constraint("ck_decisions_confidence", "decisions", type_="check")
    op.drop_column("decisions", "expected")
    op.drop_column("decisions", "confidence")
