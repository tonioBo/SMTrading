"""real trading: live link state, live audit (append-only), live fuse, flatten flag

Revision ID: 0018
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_ACTIONS = (
    "keys_saved", "test_ok", "test_failed", "dry_run_on", "dry_run_off", "armed", "disarmed",
    "portfolio_linked", "portfolio_unlinked", "reduce_only_auto", "resumed", "kill_switch",
    "flatten", "order_sent",
)


def upgrade() -> None:
    op.add_column("broker_links", sa.Column("live_state", sa.String(16)))
    op.add_column("broker_links", sa.Column("armed_at", sa.DateTime(timezone=True)))
    op.add_column("broker_links", sa.Column("disarmed_at", sa.DateTime(timezone=True)))
    op.add_column("broker_links", sa.Column("armed_by", sa.String(128)))
    op.add_column("broker_links", sa.Column("disarmed_reason", sa.String(200)))
    op.add_column("broker_links", sa.Column("account_fingerprint", sa.String(64)))
    op.add_column("broker_links", sa.Column("last_account_snapshot", postgresql.JSONB()))
    op.add_column("broker_links", sa.Column("last_snapshot_at", sa.DateTime(timezone=True)))
    op.add_column("broker_links", sa.Column("last_test_at", sa.DateTime(timezone=True)))
    op.add_column(
        "broker_links",
        sa.Column("health_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_broker_links_live_state", "broker_links",
        "live_state IS NULL OR live_state IN ('configured', 'dry_run', 'armed')",
    )

    op.add_column(
        "risk_profiles",
        sa.Column("max_live_orders_per_hour", sa.Integer(), nullable=False, server_default="30"),
    )
    op.create_check_constraint(
        "ck_risk_profiles_max_live_orders_per_hour", "risk_profiles",
        "max_live_orders_per_hour BETWEEN 1 AND 1000",
    )

    op.add_column(
        "orders",
        sa.Column("live_flatten", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "live_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("link_id", sa.UUID(), sa.ForeignKey("broker_links.id"), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id")),
        sa.Column("order_id", sa.UUID(), sa.ForeignKey("orders.id")),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(300)),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in _ACTIONS) + ")",
            name="ck_live_audit_action",
        ),
    )
    op.create_index("ix_live_audit_link_id", "live_audit", ["link_id", "id"])
    op.create_index("ix_live_audit_portfolio_id", "live_audit", ["portfolio_id", "id"])
    op.execute("""
        CREATE FUNCTION smt_live_audit_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'LIVE_AUDIT_APPEND_ONLY: % on live_audit is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER live_audit_append_only BEFORE UPDATE OR DELETE ON live_audit "
        "FOR EACH ROW EXECUTE FUNCTION smt_live_audit_append_only()"
    )
    op.execute(
        "CREATE TRIGGER live_audit_no_truncate BEFORE TRUNCATE ON live_audit "
        "FOR EACH STATEMENT EXECUTE FUNCTION smt_live_audit_append_only()"
    )


def downgrade() -> None:
    op.drop_table("live_audit")  # drops its triggers
    op.execute("DROP FUNCTION smt_live_audit_append_only()")
    op.drop_column("orders", "live_flatten")
    op.drop_constraint(
        "ck_risk_profiles_max_live_orders_per_hour", "risk_profiles", type_="check"
    )
    op.drop_column("risk_profiles", "max_live_orders_per_hour")
    op.drop_constraint("ck_broker_links_live_state", "broker_links", type_="check")
    for col in (
        "health_failures", "last_test_at", "last_snapshot_at", "last_account_snapshot",
        "account_fingerprint", "disarmed_reason", "armed_by", "disarmed_at", "armed_at",
        "live_state",
    ):
        op.drop_column("broker_links", col)
