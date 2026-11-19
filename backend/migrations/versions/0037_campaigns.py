"""Campaigns, plan 21 «Nuova campagna guidata»: `campaigns` (a named test run over a
period: name, start/end dates, notes, status bozza | attiva | conclusa, who created and
closed it), `campaign_portfolios` (its portfolios, `created` when the wizard made them)
and `campaign_goals` (the goals the wizard created for it).

Downgrade: the three tables are dropped (the portfolios and the goals stay as they are).

Revision ID: 0037
"""
import sqlalchemy as sa
from alembic import op

revision = "0037"
# 0036 (plan 21, reconciliation) lives on a parallel branch: re-chained at the merge.
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status in ('bozza', 'attiva', 'conclusa')",
                           name="ck_campaigns_status"),
        sa.CheckConstraint("end_date >= start_date", name="ck_campaigns_dates"),
    )
    op.create_table(
        "campaign_portfolios",
        sa.Column("campaign_id", sa.UUID(), sa.ForeignKey("campaigns.id"), primary_key=True),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id"),
                  primary_key=True),
        sa.Column("created", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_campaign_portfolios_portfolio", "campaign_portfolios",
                    ["portfolio_id"])
    op.create_table(
        "campaign_goals",
        sa.Column("campaign_id", sa.UUID(), sa.ForeignKey("campaigns.id"), primary_key=True),
        sa.Column("goal_id", sa.UUID(), sa.ForeignKey("goals.id"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("campaign_goals")
    op.drop_index("ix_campaign_portfolios_portfolio", table_name="campaign_portfolios")
    op.drop_table("campaign_portfolios")
    op.drop_table("campaigns")
