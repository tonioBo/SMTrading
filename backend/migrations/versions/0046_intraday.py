"""Intraday trading, shared schema (mode A: agent wake on a signal; mode B: automatic rules).
Everything is off by default: portfolios.intraday_enabled / auto_rules_enabled false, so
nothing changes until they are switched on. Adds the portfolio settings, orders.day_trade,
the risk profile's safety-net margin, the broker link's day-trade state and the
`intraday_triggers` log.

Downgrade: the table, the columns and their checks are dropped.

Revision ID: 0046
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

OUTCOMES = ("pending", "woken", "telegram", "capped", "issue_open", "no_agent", "late",
            "failed")


def upgrade() -> None:
    op.add_column("portfolios", sa.Column(
        "intraday_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("portfolios", sa.Column(
        "auto_rules_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("portfolios", sa.Column(
        "intraday_timeframes", postgresql.JSONB(), nullable=False,
        server_default=sa.text("""'["15m","1h"]'::jsonb""")))
    op.add_column("portfolios", sa.Column("intraday_wake_agent_id", sa.UUID(), nullable=True))
    op.create_foreign_key("fk_portfolios_intraday_wake_agent_id", "portfolios", "agents",
                          ["intraday_wake_agent_id"], ["id"], ondelete="SET NULL")
    op.add_column("portfolios", sa.Column(
        "day_close_minutes", sa.Integer(), nullable=False, server_default=sa.text("10")))
    op.add_column("portfolios", sa.Column("max_day_trades_5d", sa.Integer(), nullable=True))
    op.add_column("portfolios", sa.Column(
        "intraday_max_wakes_per_hour", sa.Integer(), nullable=False,
        server_default=sa.text("6")))
    op.create_check_constraint("ck_portfolios_day_close_minutes", "portfolios",
                               "day_close_minutes BETWEEN 2 AND 60")
    op.create_check_constraint("ck_portfolios_max_day_trades_5d", "portfolios",
                               "max_day_trades_5d IS NULL OR max_day_trades_5d >= 0")
    op.create_check_constraint("ck_portfolios_intraday_max_wakes_per_hour", "portfolios",
                               "intraday_max_wakes_per_hour BETWEEN 1 AND 60")

    op.add_column("orders", sa.Column(
        "day_trade", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.add_column("risk_profiles", sa.Column(
        "day_close_limit_margin_pct", sa.Numeric(38, 18), nullable=False,
        server_default=sa.text("0.5")))
    op.create_check_constraint(
        "ck_risk_profiles_day_close_limit_margin_pct", "risk_profiles",
        "day_close_limit_margin_pct > 0 AND day_close_limit_margin_pct <= 5")

    op.add_column("broker_links", sa.Column("pdt_state", postgresql.JSONB(), nullable=True))
    op.add_column("broker_links", sa.Column(
        "pdt_state_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "intraday_triggers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("strategy_ref", sa.String(128), nullable=False),
        sa.Column("strategy_name", sa.String(200), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("rule", sa.String(8), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("bar_close", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Numeric(38, 18), nullable=True),
        sa.Column("threshold", sa.Numeric(38, 18), nullable=True),
        sa.Column("price", sa.Numeric(38, 18), nullable=True),
        sa.Column("rule_text", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("issue_ident", sa.String(64), nullable=True),
        sa.Column("error", sa.String(200), nullable=True),
        sa.CheckConstraint("rule IN ('entry', 'exit')", name="ck_intraday_triggers_rule"),
        sa.CheckConstraint("outcome IN (" + ", ".join(f"'{o}'" for o in OUTCOMES) + ")",
                           name="ck_intraday_triggers_outcome"),
    )
    op.create_index("ix_intraday_triggers_portfolio_created", "intraday_triggers",
                    ["portfolio_id", "created_at"])
    op.create_index("ix_intraday_triggers_portfolio_symbol_id", "intraday_triggers",
                    ["portfolio_id", "symbol", "id"])


def downgrade() -> None:
    op.drop_table("intraday_triggers")
    op.drop_column("broker_links", "pdt_state_at")
    op.drop_column("broker_links", "pdt_state")
    op.drop_constraint("ck_risk_profiles_day_close_limit_margin_pct", "risk_profiles",
                       type_="check")
    op.drop_column("risk_profiles", "day_close_limit_margin_pct")
    op.drop_column("orders", "day_trade")
    for name in ("ck_portfolios_intraday_max_wakes_per_hour", "ck_portfolios_max_day_trades_5d",
                 "ck_portfolios_day_close_minutes"):
        op.drop_constraint(name, "portfolios", type_="check")
    op.drop_constraint("fk_portfolios_intraday_wake_agent_id", "portfolios", type_="foreignkey")
    for col in ("intraday_max_wakes_per_hour", "max_day_trades_5d", "day_close_minutes",
                "intraday_wake_agent_id", "intraday_timeframes", "auto_rules_enabled",
                "intraday_enabled"):
        op.drop_column("portfolios", col)
