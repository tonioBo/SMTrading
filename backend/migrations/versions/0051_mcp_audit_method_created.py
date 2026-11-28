"""`ix_mcp_audit_log_method_created` on mcp_audit_log(method, created_at): the latest calls
of one tool (the «Fiume dei flussi» Neurograph health reads the last get_forecast calls)
without scanning the whole audit log. One small index entry per audited call.

Downgrade: the index is dropped.

Revision ID: 0051
"""
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_mcp_audit_log_method_created", "mcp_audit_log",
                    ["method", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_audit_log_method_created", table_name="mcp_audit_log")
