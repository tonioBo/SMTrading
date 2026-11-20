"""Plan 25 «Resoconto del venerdì»: an outbox row may carry a document
(`telegram_outbox.document`, the file bytes, at most 20 MB; `document_name`, its file
name). Such a row is sent with sendDocument, its `text` being the caption; the bytes are
cleared once the row is sent or skipped (the name stays, for the admin list).

Downgrade: the columns are dropped (pending documents are lost: older code would send
their captions as plain messages, so they are skipped first).

Revision ID: 0040
"""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_outbox", sa.Column("document", sa.LargeBinary(), nullable=True))
    op.add_column("telegram_outbox", sa.Column("document_name", sa.String(128),
                                               nullable=True))


def downgrade() -> None:
    op.execute("update telegram_outbox set status = 'skipped' where document_name is not null "
               "and status in ('pending', 'paused', 'failed')")
    op.drop_column("telegram_outbox", "document_name")
    op.drop_column("telegram_outbox", "document")
