"""Goals, plan 14 Task D: the kinds `max_loss` (a loss limit over the period) and
`beat_index` (beat a benchmark by `target_pct` points, 0 allowed) with the nullable
`goals.benchmark` (SPY | QQQ | VWCE.DE, required for beat_index only); `goal_alerts` (the
last status seen per goal and period, for the status-change alerts, with the goal's kind
and target it was seen for and their Paperclip issues) and `benchmark_snapshots` (the benchmark prices in EUR sampled by the worker).

Downgrade: older code knows only min and cap, so the goals of the new kinds are deleted
(their alerts with them), then the column, the tables and the checks go back to 0031.

Revision ID: 0032
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

OLD_TARGET = "target_pct > 0"
NEW_TARGET = "target_pct > 0 OR (kind = 'beat_index' AND target_pct >= 0)"


def upgrade() -> None:
    op.alter_column("goals", "kind", type_=sa.String(16), existing_nullable=False)
    op.add_column("goals", sa.Column("benchmark", sa.String(16), nullable=True))
    op.drop_constraint("ck_goals_kind", "goals", type_="check")
    op.create_check_constraint(
        "ck_goals_kind", "goals", "kind in ('min', 'cap', 'max_loss', 'beat_index')")
    op.create_check_constraint(
        "ck_goals_benchmark", "goals",
        "(kind = 'beat_index') = (benchmark IS NOT NULL)"
        " AND (benchmark IS NULL OR benchmark in ('SPY', 'QQQ', 'VWCE.DE'))")
    op.drop_constraint("ck_goals_target", "goals", type_="check")
    op.create_check_constraint("ck_goals_target", "goals", NEW_TARGET)
    op.create_table(
        "goal_alerts",
        sa.Column("goal_id", sa.UUID(), sa.ForeignKey("goals.id"), primary_key=True),
        sa.Column("period_start", sa.Date(), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_pct", sa.Numeric(9, 2), nullable=False),
        sa.Column("alerted", sa.String(24), nullable=True),
        sa.Column("near_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sent", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("issues", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        "benchmark_snapshots",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("fx_eur", sa.Numeric(20, 10), nullable=False),
        sa.Column("price_eur", sa.Numeric(20, 8), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("benchmark_snapshots")
    op.drop_table("goal_alerts")
    op.execute("DELETE FROM goals WHERE kind NOT IN ('min', 'cap')")
    op.drop_constraint("ck_goals_target", "goals", type_="check")
    op.create_check_constraint("ck_goals_target", "goals", OLD_TARGET)
    op.drop_constraint("ck_goals_benchmark", "goals", type_="check")
    op.drop_constraint("ck_goals_kind", "goals", type_="check")
    op.create_check_constraint("ck_goals_kind", "goals", "kind in ('min', 'cap')")
    op.drop_column("goals", "benchmark")
    op.alter_column("goals", "kind", type_=sa.String(8), existing_nullable=False)
