"""eToro market-data connector (plan 9 Task 14), data only: the `etoro` connector row,
off, and `etoro` appended to the existing quote/bars priority rows of US stocks and
crypto (the seed never touches existing rows; the admin's order stays, eToro goes last)

Revision ID: 0028
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

# the values str_enum writes (AssetClass / Capability `.value`, see 0003_connectors)
_PAIRS = "('us_equity', 'quote'), ('us_equity', 'bars'), ('crypto', 'quote'), ('crypto', 'bars')"


def upgrade() -> None:
    op.execute(
        "INSERT INTO connectors (name, enabled, health_status) VALUES ('etoro', false, 'unknown')"
        " ON CONFLICT (name) DO NOTHING"
    )
    op.execute(
        "UPDATE provider_priorities SET providers = providers || '[\"etoro\"]'::jsonb"
        f" WHERE (asset_class, capability) IN ({_PAIRS})"
        " AND NOT providers @> '[\"etoro\"]'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE provider_priorities SET providers = providers - 'etoro'"
        " WHERE providers @> '[\"etoro\"]'::jsonb"
    )
    op.execute("DELETE FROM connectors WHERE name = 'etoro'")
