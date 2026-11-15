"""Portal users: role (admin | viewer, existing users are admins), forced password
change after a temporary password, deactivation (no physical delete), creator

Revision ID: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column("role", sa.String(16), nullable=False,
                                           server_default="admin"))
    op.add_column("admin_users", sa.Column("must_change_password", sa.Boolean(),
                                           nullable=False, server_default="false"))
    op.add_column("admin_users", sa.Column("disabled_at", sa.DateTime(timezone=True)))
    op.add_column("admin_users", sa.Column("created_by", sa.String(254)))
    op.create_check_constraint("ck_admin_users_role", "admin_users",
                               "role in ('admin','viewer')")


def downgrade() -> None:
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.drop_column("admin_users", "created_by")
    op.drop_column("admin_users", "disabled_at")
    op.drop_column("admin_users", "must_change_password")
    op.drop_column("admin_users", "role")
