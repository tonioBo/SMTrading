"""Crypto news RSS connector (plan 10 Task B), data only: the keyless `rssnews` connector
row (CoinDesk + Cointelegraph feeds), on, and `rssnews` put first in the existing crypto
news priority row (the seed never touches existing rows; a row that already lists it stays
as it is)

Revision ID: 0029
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO connectors (name, enabled, health_status) VALUES ('rssnews', true, 'unknown')"
        " ON CONFLICT (name) DO NOTHING"
    )
    op.execute(
        "UPDATE provider_priorities SET providers = '[\"rssnews\"]'::jsonb || providers"
        " WHERE asset_class = 'crypto' AND capability = 'news'"
        " AND NOT providers @> '[\"rssnews\"]'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE provider_priorities SET providers = providers - 'rssnews'"
        " WHERE providers @> '[\"rssnews\"]'::jsonb"
    )
    op.execute("DELETE FROM connectors WHERE name = 'rssnews'")
