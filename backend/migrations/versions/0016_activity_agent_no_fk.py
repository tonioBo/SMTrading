"""activity.agent_id without a foreign key; index on activity.ts

Recording an activity must never abort the business transaction it belongs to: the
agent id is kept as given (it may name an agent that does not exist). The index on
ts serves the feed's `since` filter.

Revision ID: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("activity_agent_id_fkey", "activity", type_="foreignkey")
    op.create_index("ix_activity_ts", "activity", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_activity_ts", table_name="activity")
    op.execute(sa.text(
        "update activity set agent_id = null"
        " where agent_id is not null and agent_id not in (select id from agents)"
    ))
    op.create_foreign_key(
        "activity_agent_id_fkey", "activity", "agents", ["agent_id"], ["id"],
        ondelete="SET NULL",
    )
