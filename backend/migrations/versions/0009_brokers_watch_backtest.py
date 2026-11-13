"""brokers, watchlists, alerts, backtests; orders.partially_filled

Revision ID: 0009
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

NUM = sa.Numeric(38, 18)
_OLD_WHERE = sa.text("status IN ('new', 'held', 'queued', 'pending', 'triggered')")
_NEW_WHERE = sa.text(
    "status IN ('new', 'held', 'queued', 'pending', 'triggered', 'partially_filled')"
)


def _uuid_pk():
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _ts(name, nullable=False):
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True))
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "broker_links",
        _uuid_pk(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("config_encrypted", sa.LargeBinary()),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_error", sa.Text()),
        _ts("created_at"),
    )
    op.create_table(
        "reconciliations",
        _uuid_pk(),
        sa.Column("broker_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("broker_links.id"), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("portfolios.id"), nullable=False),
        _ts("ts"),
        sa.Column("diffs", postgresql.JSONB(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_reconciliations_broker_link_id", "reconciliations", ["broker_link_id"])
    op.create_index("ix_reconciliations_portfolio_id", "reconciliations", ["portfolio_id"])

    op.add_column(
        "portfolios",
        sa.Column("broker_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("broker_links.id")),
    )
    op.add_column(
        "orders",
        sa.Column("broker_link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("broker_links.id")),
    )
    op.add_column("orders", sa.Column("broker_status", sa.String(32)))
    op.add_column(
        "orders",
        sa.Column("broker_replicated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("orders", sa.Column("broker_last_sync_at", sa.DateTime(timezone=True)))

    # open-order partial indexes now include partially_filled
    op.drop_index("ix_orders_open_created_at", table_name="orders")
    op.drop_index("ix_orders_open_expires_at", table_name="orders")
    op.create_index("ix_orders_open_expires_at", "orders", ["expires_at"], postgresql_where=_NEW_WHERE)
    op.create_index("ix_orders_open_created_at", "orders", ["created_at"], postgresql_where=_NEW_WHERE)

    op.create_table(
        "watchlists",
        _uuid_pk(),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("agent_id", "name", name="uq_watchlists_agent_name"),
    )
    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("watchlists.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("symbol", sa.String(64), primary_key=True),
    )
    op.create_table(
        "alerts",
        _uuid_pk(),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("condition", sa.String(32), nullable=False),
        sa.Column("value", NUM, nullable=False),
        sa.Column("ref_price", NUM),
        sa.Column("status", sa.String(16), nullable=False),
        _ts("created_at"),
        _ts("triggered_at", nullable=True),
        sa.Column("triggered_price", NUM),
        sa.Column("note", sa.String(512)),
    )
    op.create_index("ix_alerts_agent_status", "alerts", ["agent_id", "status"])
    op.create_index("ix_alerts_symbol_status", "alerts", ["symbol", "status"])

    op.create_table(
        "backtests",
        _uuid_pk(),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_cash", NUM, nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("portfolios.id")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("finished_at", nullable=True),
    )
    op.create_index("ix_backtests_status_created", "backtests", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("backtests")
    op.drop_table("alerts")
    op.drop_table("watchlist_items")
    op.drop_table("watchlists")
    op.drop_index("ix_orders_open_created_at", table_name="orders")
    op.drop_index("ix_orders_open_expires_at", table_name="orders")
    op.create_index("ix_orders_open_expires_at", "orders", ["expires_at"], postgresql_where=_OLD_WHERE)
    op.create_index("ix_orders_open_created_at", "orders", ["created_at"], postgresql_where=_OLD_WHERE)
    for c in ("broker_last_sync_at", "broker_replicated", "broker_status", "broker_link_id"):
        op.drop_column("orders", c)
    op.drop_column("portfolios", "broker_link_id")
    op.drop_table("reconciliations")
    op.drop_table("broker_links")
