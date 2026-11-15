"""Structured fields of a logged decision: outcome (one of five Italian values), reason
and subject, all optional

Revision ID: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# activity.models.DecisionOutcome at this revision
_OUTCOMES = "'approvata', 'respinta', 'sospesa', 'modificata', 'informativa'"


def upgrade() -> None:
    op.add_column("decisions", sa.Column("outcome", sa.String(16)))
    op.add_column("decisions", sa.Column("reason", sa.String(300)))
    op.add_column("decisions", sa.Column("subject", sa.String(200)))
    op.create_check_constraint(
        "ck_decisions_outcome", "decisions", f"outcome IS NULL OR outcome in ({_OUTCOMES})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_decisions_outcome", "decisions", type_="check")
    op.drop_column("decisions", "subject")
    op.drop_column("decisions", "reason")
    op.drop_column("decisions", "outcome")
