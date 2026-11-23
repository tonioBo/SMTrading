"""Portfolios: an optional short `code` (2-6 uppercase letters/digits) the agents use in
task titles and messages instead of a hardcoded table in their instructions; unique among
the portfolios that are not archived. The current active ones get the codes the agents
already used: «Portafoglio Alpaca» AGG, «Portafoglio eToro» PRU, «Portafoglio Moderato»
MOD (only where the portfolio exists, is not archived and the code is free); every other
portfolio has none (the admin sets it in the portal).

Downgrade: the column, its check and its index are dropped.

Revision ID: 0045
"""
import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

CODES = {
    "Portafoglio Alpaca": "AGG",
    "Portafoglio eToro": "PRU",
    "Portafoglio Moderato": "MOD",
}


def upgrade() -> None:
    op.add_column("portfolios", sa.Column("code", sa.String(6), nullable=True))
    op.create_check_constraint("ck_portfolios_code", "portfolios",
                               "code ~ '^[A-Z0-9]{2,6}$'")
    op.create_index("uq_portfolios_code_live", "portfolios", ["code"], unique=True,
                    postgresql_where=sa.text("status <> 'archived' AND code IS NOT NULL"))
    for name, code in CODES.items():
        op.execute(sa.text(
            "UPDATE portfolios SET code = :code WHERE name = :name AND status <> 'archived'"
            " AND NOT EXISTS (SELECT 1 FROM portfolios p2 WHERE p2.code = :code"
            " AND p2.status <> 'archived')"
        ).bindparams(code=code, name=name))


def downgrade() -> None:
    op.drop_index("uq_portfolios_code_live", table_name="portfolios")
    op.drop_constraint("ck_portfolios_code", "portfolios", type_="check")
    op.drop_column("portfolios", "code")
