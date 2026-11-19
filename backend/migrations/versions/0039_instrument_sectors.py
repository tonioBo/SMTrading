"""Plan 22 «Pagina Mercati»: `instruments.sector` (Italian GICS-like sector, or ETF /
Crypto / Cambi), `instruments.industry` (the provider's industry; for ETFs the asset
type), `instruments.indices` (the index codes it belongs to: SP500, NDX, FTSEMIB, DAX,
CAC40, MDAX) and `instruments.created_at` (when the row was added: NULL for the rows that
existed before, so «aggiunti di recente» never lists the whole catalog).

The seed's metadata backfill fills the NULL sector / industry / indices from the catalog
CSVs; it never overwrites a value (admin edits win).

Downgrade: the columns and their indexes are dropped (older code does not read them).

Revision ID: 0039
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("sector", sa.String(64), nullable=True))
    op.add_column("instruments", sa.Column("industry", sa.String(128), nullable=True))
    op.add_column("instruments", sa.Column(
        "indices", postgresql.ARRAY(sa.String(16)), nullable=True))
    # added without a default (existing rows stay NULL), then new rows get now()
    op.add_column("instruments", sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("instruments", "created_at", server_default=sa.func.now())
    op.create_index("ix_instruments_sector", "instruments", ["sector"])
    op.create_index("ix_instruments_indices", "instruments", ["indices"],
                    postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_instruments_indices", table_name="instruments")
    op.drop_index("ix_instruments_sector", table_name="instruments")
    op.drop_column("instruments", "created_at")
    op.drop_column("instruments", "indices")
    op.drop_column("instruments", "industry")
    op.drop_column("instruments", "sector")
