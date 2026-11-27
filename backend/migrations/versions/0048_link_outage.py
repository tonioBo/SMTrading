"""The last outage of a paper/demo broker link (brokers.health): `broker_links.outage_since`
(its first unanswered call) and `outage_until` (the first answer after it, null while it
lasts). BrokerSync refuses a never-sent market order that waited through more than
MATCH_WINDOW of it.

Downgrade: the two columns are dropped.

Revision ID: 0048
"""
import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("broker_links", sa.Column(
        "outage_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("broker_links", sa.Column(
        "outage_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("broker_links", "outage_until")
    op.drop_column("broker_links", "outage_since")
