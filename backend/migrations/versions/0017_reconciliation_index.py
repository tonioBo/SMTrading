"""index on reconciliations (broker_link_id, portfolio_id, ts)

`reconcile` reads the previous reconciliation of each portfolio on the link.

Revision ID: 0017
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_reconciliations_link_portfolio_ts", "reconciliations",
        ["broker_link_id", "portfolio_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliations_link_portfolio_ts", table_name="reconciliations")
