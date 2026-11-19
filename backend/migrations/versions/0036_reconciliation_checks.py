"""Plan 21 «Riconciliazione con il broker»: `reconciliation_checks`, one row per check of a
broker-linked portfolio (positions, cash and open orders against the broker account, item
by item, with the status ok | warning | mismatch | unverifiable).

Downgrade: the table is dropped (older code does not run these checks; the alerts already
sent stay in the activity feed).

Revision ID: 0036
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("broker_link_id", sa.UUID(), sa.ForeignKey("broker_links.id"),
                  nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.String(500), nullable=True),
        sa.CheckConstraint("status in ('ok','warning','mismatch','unverifiable')",
                           name="ck_reconciliation_checks_status"),
        sa.CheckConstraint("trigger in ('schedule', 'manual')",
                           name="ck_reconciliation_checks_trigger"),
    )
    op.create_index("ix_reconciliation_checks_portfolio_ts", "reconciliation_checks",
                    ["portfolio_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_reconciliation_checks_portfolio_ts", table_name="reconciliation_checks")
    op.drop_table("reconciliation_checks")
