"""Reference FX rates of a real-money link (several currencies on real money): the last
rates at which the linked portfolio's cash matched the broker's

Revision ID: 0026
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broker_links", sa.Column("fx_ref_rates", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("broker_links", "fx_ref_rates")
