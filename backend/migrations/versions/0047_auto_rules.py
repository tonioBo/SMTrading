"""Intraday mode B (automatic rules): the rules, their versions and their trades, the
portfolio brakes (`portfolios.auto_brakes`) and `orders.auto_rule_id`. Nothing runs until
a rule is activated on a portfolio with `auto_rules_enabled` (off by default).

At most one active rule per (portfolio, symbol): partial unique index
`uq_auto_rules_active_symbol`. A trigger is recorded once: `uq_auto_trades_rule_trigger`.

Downgrade: the tables and the two columns are dropped.

Revision ID: 0047
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

NUM = sa.Numeric(38, 18)
TS = sa.DateTime(timezone=True)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.add_column("portfolios", sa.Column(
        "auto_brakes", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))

    op.create_table(
        "auto_rules",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("portfolio_id", sa.UUID(),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(32), sa.ForeignKey("instruments.symbol"),
                  nullable=False),
        sa.Column("model", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status_reason", sa.String(200), nullable=True),
        sa.Column("status_changed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "model IN (" + _in(("breakout", "mean_reversion", "vwap_pullback")) + ")",
            name="ck_auto_rules_model"),
        sa.CheckConstraint(
            "status IN (" + _in(("draft", "active", "paused", "braked")) + ")",
            name="ck_auto_rules_status"),
    )
    op.create_index("uq_auto_rules_active_symbol", "auto_rules", ["portfolio_id", "symbol"],
                    unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_index("ix_auto_rules_status", "auto_rules", ["status"])

    op.create_table(
        "auto_rule_versions",
        sa.Column("rule_id", sa.UUID(), sa.ForeignKey("auto_rules.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("qty", NUM, nullable=True),
        sa.Column("notional", NUM, nullable=True),
        sa.Column("brakes", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.CheckConstraint("(qty IS NULL) <> (notional IS NULL)",
                           name="ck_auto_rule_versions_qty_xor_notional"),
        sa.CheckConstraint(
            "(qty IS NULL OR qty > 0) AND (notional IS NULL OR notional > 0)",
            name="ck_auto_rule_versions_size_positive"),
    )

    op.add_column("orders", sa.Column("auto_rule_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_orders_auto_rule_id", "orders", "auto_rules",
                          ["auto_rule_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_auto_rule_id", "orders", ["auto_rule_id"])

    op.create_table(
        "auto_trades",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("rule_id", sa.UUID(), sa.ForeignKey("auto_rules.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.UUID(),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("trigger_key", sa.String(64), nullable=False),
        sa.Column("trigger_at", TS, nullable=False),
        sa.Column("trigger_price", NUM, nullable=False),
        sa.Column("trigger_bid", NUM, nullable=True),
        sa.Column("trigger_ask", NUM, nullable=True),
        sa.Column("trigger_values", postgresql.JSONB(), nullable=False),
        sa.Column("expected_price", NUM, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entry_order_id", sa.UUID(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("broker_sent_at", TS, nullable=True),
        sa.Column("filled_at", TS, nullable=True),
        sa.Column("fill_ts_source", sa.String(8), nullable=True),
        sa.Column("fill_price", NUM, nullable=True),
        sa.Column("qty", NUM, nullable=True),
        sa.Column("exit_order_id", sa.UUID(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("exit_reason", sa.String(16), nullable=True),
        sa.Column("exit_trigger_at", TS, nullable=True),
        sa.Column("exit_expected", NUM, nullable=True),
        sa.Column("exit_filled_at", TS, nullable=True),
        sa.Column("exit_price", NUM, nullable=True),
        sa.Column("fees", NUM, nullable=False, server_default=sa.text("0")),
        sa.Column("fees_currency", sa.String(16), nullable=True),
        sa.Column("net_result", NUM, nullable=True),
        sa.Column("net_currency", sa.String(16), nullable=True),
        sa.Column("entry_slippage_bps", NUM, nullable=True),
        sa.Column("exit_slippage_bps", NUM, nullable=True),
        sa.Column("ms_trigger_to_send", sa.Integer(), nullable=True),
        sa.Column("ms_send_to_broker", sa.Integer(), nullable=True),
        sa.Column("ms_broker_to_fill", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("rule_id", "trigger_key", name="uq_auto_trades_rule_trigger"),
        sa.CheckConstraint(
            "status IN (" + _in(("sent", "open", "closed", "canceled", "rejected")) + ")",
            name="ck_auto_trades_status"),
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN ("
            + _in(("stop", "target", "time", "session", "manual", "day_close")) + ")",
            name="ck_auto_trades_exit_reason"),
        sa.CheckConstraint(
            "fill_ts_source IS NULL OR fill_ts_source IN ('broker', 'local')",
            name="ck_auto_trades_fill_ts_source"),
    )
    op.create_index("ix_auto_trades_portfolio_trigger_at", "auto_trades",
                    ["portfolio_id", "trigger_at"])
    op.create_index("ix_auto_trades_rule_status", "auto_trades", ["rule_id", "status"])


def downgrade() -> None:
    op.drop_table("auto_trades")
    op.drop_index("ix_orders_auto_rule_id", table_name="orders")
    op.drop_constraint("fk_orders_auto_rule_id", "orders", type_="foreignkey")
    op.drop_column("orders", "auto_rule_id")
    op.drop_table("auto_rule_versions")
    op.drop_table("auto_rules")
    op.drop_column("portfolios", "auto_brakes")
