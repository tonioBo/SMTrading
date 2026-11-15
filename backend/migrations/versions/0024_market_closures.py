"""Manual market closures («Chiusure straordinarie»): a whole closed day or an early
close (local time of the exchange), one per market and day

Revision ID: 0024
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# the calendar markets of instruments.market_hours.CALENDARS at this revision
_MARKETS = "'NASDAQ', 'NYSE', 'MTA', 'XETRA', 'EURONEXT_PARIS', 'LSE'"


def upgrade() -> None:
    op.create_table(
        "market_closures",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("close_time", sa.Time()),
        sa.Column("note", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("market", "day", name="uq_market_closures_market_day"),
        sa.CheckConstraint(f"market in ({_MARKETS})", name="ck_market_closures_market"),
        sa.CheckConstraint("kind in ('closed', 'early_close')", name="ck_market_closures_kind"),
        sa.CheckConstraint(
            "(kind = 'early_close') = (close_time IS NOT NULL)",
            name="ck_market_closures_close_time",
        ),
    )


def downgrade() -> None:
    op.drop_table("market_closures")
