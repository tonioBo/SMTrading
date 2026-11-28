"""The nightly export to Neurograph: `neurograph_exports`, the last send of each file
(type decisioni|ordini|esecuzioni, Rome day, version) with its result (confirmed, rejected,
failed), for idempotency, the catch-up of missed days and the «Fiume dei flussi»;
`neurograph_export_nights`, the outcome of each night's run (outage streak alerts);
`portfolios.neurograph_export` (default false): a portfolio outside the totals whose data
is still exported, tagged `prova_intraday`.

Downgrade: the tables and the column are dropped (the next night sends the last 30 days again; Neurograph
answers «già presente» for what it already has).

Revision ID: 0050
"""
import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "neurograph_exports",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status in ('confirmed','rejected','failed')",
                           name="ck_neurograph_exports_status"),
        sa.CheckConstraint("kind in ('decisioni', 'ordini', 'esecuzioni')",
                           name="ck_neurograph_exports_kind"),
    )
    op.create_index("ix_neurograph_exports_sent_at", "neurograph_exports", ["sent_at"])
    op.create_table(
        "neurograph_export_nights",
        sa.Column("night", sa.Date(), primary_key=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("outcome in ('ok','outage','unauthorized')",
                           name="ck_neurograph_export_nights_outcome"),
    )
    op.add_column("portfolios", sa.Column("neurograph_export", sa.Boolean(), nullable=False,
                                          server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("portfolios", "neurograph_export")
    op.drop_table("neurograph_export_nights")
    op.drop_index("ix_neurograph_exports_sent_at", table_name="neurograph_exports")
    op.drop_table("neurograph_exports")
