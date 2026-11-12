"""engine: orders, fills, positions, risk, jobs

Revision ID: 0004
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

NUM = sa.Numeric(38, 18)
UUID = postgresql.UUID(as_uuid=True)


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "risk_profiles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("max_order_notional_eur", NUM),
        sa.Column("max_position_pct", NUM),
        sa.Column("max_daily_loss_pct", NUM),
        sa.Column("max_daily_loss_eur", NUM),
        sa.Column("allowed_symbols", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_asset_classes", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_markets", postgresql.JSONB(), nullable=False),
        sa.Column("short_allowed", sa.Boolean(), nullable=False),
    )
    op.add_column("portfolios", sa.Column("risk_profile_id", UUID, sa.ForeignKey("risk_profiles.id")))
    op.add_column("portfolios", sa.Column("reduce_only_reason", sa.String(64)))
    op.add_column("portfolios", _ts("reduce_only_until"))

    op.create_table(
        "orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("portfolio_id", UUID, sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("client_order_id", sa.String(128)),
        sa.Column("symbol", sa.String(64), sa.ForeignKey("instruments.symbol"), nullable=False),
        sa.Column("side", sa.String(32), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("tif", sa.String(32), nullable=False),
        sa.Column("qty", NUM),
        sa.Column("notional", NUM),
        sa.Column("limit_price", NUM),
        sa.Column("stop_price", NUM),
        sa.Column("trail_pct", NUM),
        sa.Column("trail_amount", NUM),
        sa.Column("trail_ref_price", NUM),
        sa.Column("filled_qty", NUM, nullable=False),
        sa.Column("avg_fill_price", NUM),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("parent_id", UUID, sa.ForeignKey("orders.id")),
        sa.Column("oco_group_id", UUID),
        sa.Column("role", sa.String(16)),
        _ts("expires_at"),
        sa.Column("reject_code", sa.String(64)),
        sa.Column("reject_message", sa.String(512)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("broker_order_id", sa.String(128)),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("portfolio_id", "client_order_id", name="uq_orders_portfolio_client"),
        sa.CheckConstraint("(qty IS NULL) <> (notional IS NULL)", name="ck_orders_qty_xor_notional"),
    )
    op.create_index("ix_orders_symbol_status", "orders", ["symbol", "status"])
    op.create_index("ix_orders_portfolio_status", "orders", ["portfolio_id", "status"])

    op.create_table(
        "fills",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("order_id", UUID, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("portfolio_id", UUID, sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("symbol", sa.String(64), sa.ForeignKey("instruments.symbol"), nullable=False),
        sa.Column("side", sa.String(32), nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("price", NUM, nullable=False),
        sa.Column("quote_bid", NUM),
        sa.Column("quote_ask", NUM),
        sa.Column("quote_last", NUM),
        _ts("quote_ts"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("fee_amount", NUM, nullable=False),
        sa.Column("fee_currency", sa.String(16), nullable=False),
        sa.Column("fee_rule_id", UUID),
        sa.Column("journal_id", UUID, sa.ForeignKey("journals.id"), nullable=False),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])
    op.create_index("ix_fills_portfolio_id", "fills", ["portfolio_id"])

    op.create_table(
        "positions",
        sa.Column("portfolio_id", UUID, sa.ForeignKey("portfolios.id"), primary_key=True),
        sa.Column("symbol", sa.String(64), sa.ForeignKey("instruments.symbol"), primary_key=True),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("avg_price", NUM, nullable=False),
        sa.Column("realized_pnl", NUM, nullable=False),
        _ts("opened_at"),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "interest_rates",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("annual_pct", NUM, nullable=False),
    )
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        _ts("updated_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("portfolio_id", UUID, sa.ForeignKey("portfolios.id"), nullable=False),
        _ts("ts", nullable=False, server_default=sa.func.now()),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("local_date", sa.Date()),
        sa.Column("equity_eur", NUM, nullable=False),
        sa.Column("cash_eur", NUM, nullable=False),
        sa.Column("exposure_eur", NUM, nullable=False),
        sa.Column("margin_used_eur", NUM, nullable=False),
        sa.UniqueConstraint(
            "portfolio_id", "kind", "local_date", name="uq_equity_snapshots_day_open"
        ),
    )
    op.create_index("ix_equity_snapshots_portfolio_ts", "equity_snapshots", ["portfolio_id", "ts"])
    op.create_table(
        "job_runs",
        sa.Column("job", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        _ts("ran_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("portfolio_id", UUID, sa.ForeignKey("portfolios.id")),
        sa.Column("agent_id", sa.String(128)),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    for t in (
        "notifications", "job_runs", "equity_snapshots", "app_settings", "system_state",
        "interest_rates", "positions", "fills", "orders",
    ):
        op.drop_table(t)
    op.drop_column("portfolios", "reduce_only_until")
    op.drop_column("portfolios", "reduce_only_reason")
    op.drop_column("portfolios", "risk_profile_id")
    op.drop_table("risk_profiles")
