"""«Istruzioni agenti»: agent_docs and the append-only agent_doc_versions

Revision ID: 0020
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_docs",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
    )
    op.create_table(
        "agent_doc_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("slug", sa.String(64), sa.ForeignKey("agent_docs.slug"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(254), nullable=False),
        sa.Column("note", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("slug", "version", name="uq_agent_doc_versions_slug_version"),
        sa.CheckConstraint(
            "octet_length(body) <= 204800", name="ck_agent_doc_versions_body_size"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION smt_block_agent_doc_version_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'AGENT_DOC_VERSION_IMMUTABLE';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER agent_doc_versions_no_update_delete BEFORE UPDATE OR DELETE"
        " ON agent_doc_versions FOR EACH ROW"
        " EXECUTE FUNCTION smt_block_agent_doc_version_mutation()"
    )


def downgrade() -> None:
    op.drop_table("agent_doc_versions")
    op.execute("DROP FUNCTION smt_block_agent_doc_version_mutation()")
    op.drop_table("agent_docs")
