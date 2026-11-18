"""Goals, plan 17 «Chiusura dei piani»: `goals.closes_positions` (the goal is a «piano»:
the positions open at the end of its period are closed) with `goals.plan_since` (when it
last became one), and `plan_closures` (each position of an ended period of a piano and how
it is closed: pending, grace with its deadline, closing with SMTrading's order, closed).
The active recurring weekly goal on the total («settimanale», the one migrated from the old
setting; the earliest if several) becomes a piano from now (owner decision, plan 17), when
no other active piano exists.

Downgrade: the table and the columns are dropped (older code closes nothing at a period's
end; an open grace is simply forgotten).

Revision ID: 0035
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("goals", sa.Column("closes_positions", sa.Boolean(), nullable=False,
                                     server_default=sa.false()))
    op.add_column("goals", sa.Column("plan_since", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "plan_closures",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("goal_id", sa.UUID(), sa.ForeignKey("goals.id"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("qty", sa.Numeric(38, 18), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("grace_days", sa.Integer(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("closed_by", sa.String(16), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("goal_id", "period_start", "portfolio_id", "symbol",
                            name="uq_plan_closures_position"),
        sa.CheckConstraint("state in ('pending', 'grace', 'closing', 'closed')",
                           name="ck_plan_closures_state"),
    )
    op.create_index("ix_plan_closures_open", "plan_closures", ["portfolio_id", "symbol"],
                    postgresql_where=sa.text("state <> 'closed'"))
    op.execute(
        "UPDATE goals SET closes_positions = true, plan_since = now() WHERE id = ("
        " SELECT id FROM goals WHERE active AND recurring AND horizon = 'week'"
        " AND scope = 'total' AND name = 'settimanale'"
        " ORDER BY created_at, id LIMIT 1)"
        " AND NOT EXISTS (SELECT 1 FROM goals WHERE active AND closes_positions)")


def downgrade() -> None:
    op.drop_index("ix_plan_closures_open", table_name="plan_closures")
    op.drop_table("plan_closures")
    op.drop_column("goals", "plan_since")
    op.drop_column("goals", "closes_positions")
