"""instruments: source (catalog | manual | auto) and added_by

Revision ID: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# the instruments the seed inserted before the catalog existed
_SEEDED = (
    "AAPL", "MSFT", "NVDA", "SPY", "ENI.MI", "ISP.MI", "SAP.DE", "VWCE.DE", "BTC-USD",
    "ETH-USD", "EURUSD", "GBPUSD",
)


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
    )
    op.add_column("instruments", sa.Column("added_by", sa.String(128)))
    op.execute(
        sa.text("UPDATE instruments SET source = 'catalog' WHERE symbol = ANY(:s)").bindparams(
            sa.bindparam("s", list(_SEEDED), type_=sa.ARRAY(sa.String()))
        )
    )


def downgrade() -> None:
    """Older code has no EURONEXT_PARIS market (market_status would raise NOT_FOUND): those
    instruments are deactivated, not deleted (orders and the ledger reference them)."""
    op.execute("UPDATE instruments SET active = false WHERE market = 'EURONEXT_PARIS'")
    op.drop_column("instruments", "added_by")
    op.drop_column("instruments", "source")
