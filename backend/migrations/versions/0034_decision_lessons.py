"""Decisions, plan 17: the kinds `lesson` (an error or avoidable loss and what to do
next time) and `team_rule` (a team rule made from lessons, active or retired), with the
index `ix_decisions_kind` (kind, id) for the get_decisions kind filter.

Downgrade: older code knows only the 0015 kinds, so the lessons and team rules become
`other` (kept, the original kind saved in details.former_kind), then the index and the
check go back to 0033.

Revision ID: 0034
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_OLD = "'proposal','approval','veto','allocation','strategy','risk_rule','other'"
_NEW = _OLD + ",'lesson','team_rule'"


def upgrade() -> None:
    op.drop_constraint("ck_decisions_kind", "decisions", type_="check")
    op.create_check_constraint("ck_decisions_kind", "decisions", f"kind in ({_NEW})")
    op.create_index("ix_decisions_kind", "decisions", ["kind", "id"])


def downgrade() -> None:
    op.drop_index("ix_decisions_kind", table_name="decisions")
    op.execute(
        "UPDATE decisions SET details = details || jsonb_build_object('former_kind', kind),"
        " kind = 'other' WHERE kind IN ('lesson', 'team_rule')")
    op.drop_constraint("ck_decisions_kind", "decisions", type_="check")
    op.create_check_constraint("ck_decisions_kind", "decisions", f"kind in ({_OLD})")
