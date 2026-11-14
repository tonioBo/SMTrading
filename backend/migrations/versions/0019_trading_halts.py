"""trading halts («blocco aperture») and the agents' halt permission

Revision ID: 0019
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("can_halt", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "trading_halts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("set_by", sa.String(200), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cleared_by", sa.String(200)),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("scope IN ('global', 'portfolio')", name="ck_trading_halts_scope"),
        sa.CheckConstraint(
            "(scope = 'global') = (portfolio_id IS NULL)", name="ck_trading_halts_portfolio"
        ),
        sa.CheckConstraint("active = (cleared_at IS NULL)", name="ck_trading_halts_cleared"),
    )
    op.create_index(
        "uq_trading_halts_active_global", "trading_halts", ["scope"], unique=True,
        postgresql_where=sa.text("active AND scope = 'global'"),
    )
    op.create_index(
        "uq_trading_halts_active_portfolio", "trading_halts", ["portfolio_id"], unique=True,
        postgresql_where=sa.text("active AND scope = 'portfolio'"),
    )
    op.create_index("ix_trading_halts_set_at", "trading_halts", ["set_at"])


def downgrade() -> None:
    op.drop_table("trading_halts")
    op.drop_column("agents", "can_halt")
