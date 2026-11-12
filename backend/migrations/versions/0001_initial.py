"""initial schema with immutable ledger triggers

Revision ID: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NUM = sa.Numeric(38, 18)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("base_currency", sa.String(16), nullable=False),
        sa.Column("leverage", NUM, nullable=False),
        sa.Column("maintenance_margin_pct", NUM, nullable=False),
        sa.Column("warning_margin_pct", NUM, nullable=False),
        sa.Column("slippage_bps", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("lot_size", NUM, nullable=False),
        sa.Column("tick_size", NUM, nullable=False),
        sa.Column("shortable", sa.Boolean, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("provider_symbols", JSONB, nullable=False),
    )
    op.create_table(
        "fee_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("scope_ref", sa.String(64)),
        sa.Column("side", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("fixed", NUM, nullable=False),
        sa.Column("pct", NUM, nullable=False),
        sa.Column("min_fee", NUM),
        sa.Column("max_fee", NUM),
        sa.Column("tiers", JSONB),
        sa.Column("fx_pct", NUM, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("portfolio_id", UUID(as_uuid=True), sa.ForeignKey("portfolios.id")),
        sa.Column("asset", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.UniqueConstraint(
            "portfolio_id", "asset", "kind",
            name="uq_accounts_portfolio_asset_kind",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "journals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(), nullable=False, unique=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("order_id", UUID(as_uuid=True)),
        sa.Column("fill_id", UUID(as_uuid=True)),
        sa.Column(
            "reversed_journal_id", UUID(as_uuid=True), sa.ForeignKey("journals.id"), unique=True
        ),
        sa.Column("author_type", sa.String(16), nullable=False),
        sa.Column("author_id", sa.String(128)),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("prev_hash", sa.CHAR(64), nullable=False),
        sa.Column("hash", sa.CHAR(64), nullable=False, unique=True),
    )
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("journal_id", UUID(as_uuid=True), sa.ForeignKey("journals.id"), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("quantity", NUM, nullable=False),
        sa.Column("unit_price", NUM),
        sa.Column("price_ccy", sa.String(16)),
    )
    op.create_index("ix_journal_lines_journal_id", "journal_lines", ["journal_id"])
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"])
    op.create_table(
        "account_balances",
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("accounts.id"), primary_key=True),
        sa.Column("balance", NUM, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )

    op.execute(
        """
        CREATE FUNCTION smt_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'LEDGER_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("journals", "journal_lines"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION smt_block_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION smt_block_mutation()"
        )


def downgrade() -> None:
    for table in ("journals", "journal_lines"):
        op.execute(f"DROP TRIGGER {table}_no_update_delete ON {table}")
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
    op.execute("DROP FUNCTION smt_block_mutation()")
    for t in ("account_balances", "journal_lines", "journals", "accounts",
              "fee_rules", "instruments", "portfolios"):
        op.drop_table(t)
