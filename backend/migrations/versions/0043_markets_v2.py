"""Plan 30 «Mercati v2»: the portal's own market tables.

- `portal_favorites`: each portal user's starred instruments («I miei preferiti»);
- `portal_price_alerts`: the owner's personal price alerts (sent once to Telegram),
  apart from the agents' `alerts`;
- `market_screen_rows`: the technical figures per instrument from daily bars (the
  screener and the multi-period sector map), precomputed by the worker.

None of them is read by the agents (no MCP tool). Downgrade: the tables are dropped
(older code does not read them; the screener rows are recomputed, favourites and
personal alerts are lost).

Revision ID: 0043
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

NUM = sa.Numeric(38, 18)


def upgrade() -> None:
    op.create_table(
        "portal_favorites",
        sa.Column("admin_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        "portal_price_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("threshold", NUM, nullable=False),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("triggered_at", sa.DateTime(timezone=True)),
        sa.Column("triggered_price", NUM),
        sa.CheckConstraint("direction in ('above','below')",
                           name="ck_portal_price_alerts_direction"),
        sa.CheckConstraint("status in ('active','triggered')",
                           name="ck_portal_price_alerts_status"),
        sa.CheckConstraint("source in ('catalog','overview')",
                           name="ck_portal_price_alerts_source"),
    )
    op.create_index("ix_portal_price_alerts_status", "portal_price_alerts", ["status"])
    op.create_table(
        "market_screen_rows",
        sa.Column("symbol", sa.String(64),
                  sa.ForeignKey("instruments.symbol", ondelete="CASCADE"), primary_key=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bars", sa.Integer(), nullable=False),
        sa.Column("close", NUM, nullable=False),
        *(sa.Column(c, NUM) for c in (
            "rsi14", "sma200", "high_52w", "low_52w", "day_high", "day_low", "volume",
            "avg_volume20", "chg_1d", "chg_1w", "chg_1m", "chg_ytd")),
    )


def downgrade() -> None:
    op.drop_table("market_screen_rows")
    op.drop_index("ix_portal_price_alerts_status", table_name="portal_price_alerts")
    op.drop_table("portal_price_alerts")
    op.drop_table("portal_favorites")
