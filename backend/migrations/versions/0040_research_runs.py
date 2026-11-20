"""Plan 25 «Strumenti per il Quant»: `research_runs`, the walk-forward, parameter sweep
and portfolio backtest runs (queued and run by the worker like `backtests`, same
lifecycle columns; `kind` walk_forward | param_sweep | portfolio, `spec` the request).

Downgrade: the table is dropped (older code does not read it).

Revision ID: 0040
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("strategy", sa.String(64)),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_cash", sa.Numeric(38, 18), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_research_runs_status_created", "research_runs",
                    ["status", "created_at"])
    op.create_index("ix_research_runs_agent_created", "research_runs",
                    ["agent_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_research_runs_agent_created", table_name="research_runs")
    op.drop_index("ix_research_runs_status_created", table_name="research_runs")
    op.drop_table("research_runs")
