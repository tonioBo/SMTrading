"""ledger hardening: unique prev_hash, immutable accounts

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A second journal pointing at the same predecessor would fork the hash chain.
    op.create_unique_constraint("uq_journals_prev_hash", "journals", ["prev_hash"])
    op.execute(
        "CREATE TRIGGER accounts_no_update_delete BEFORE UPDATE OR DELETE ON accounts "
        "FOR EACH ROW EXECUTE FUNCTION smt_block_mutation()"
    )
    op.execute(
        "CREATE TRIGGER accounts_no_truncate BEFORE TRUNCATE ON accounts "
        "FOR EACH STATEMENT EXECUTE FUNCTION smt_block_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER accounts_no_truncate ON accounts")
    op.execute("DROP TRIGGER accounts_no_update_delete ON accounts")
    op.drop_constraint("uq_journals_prev_hash", "journals", type_="unique")
