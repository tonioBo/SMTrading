"""«Istruzioni agenti»: the Paperclip agent instructions, edited in the admin portal.

`agent_docs` holds one row per document (its current version number);
`agent_doc_versions` the full text of every version, append-only (a DB trigger refuses
UPDATE/DELETE): a change or a restore always adds a new version.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

MAX_BODY_BYTES = 200 * 1024
MAX_NOTE_LEN = 200


class AgentDoc(Base):
    __tablename__ = "agent_docs"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    current_version: Mapped[int] = mapped_column(Integer)


class AgentDocVersion(Base):
    __tablename__ = "agent_doc_versions"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_agent_doc_versions_slug_version"),
        CheckConstraint(f"octet_length(body) <= {MAX_BODY_BYTES}",
                        name="ck_agent_doc_versions_body_size"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), ForeignKey("agent_docs.slug"))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    # the admin's email ("seed" for the texts loaded from docs/paperclip-agents)
    author: Mapped[str] = mapped_column(String(254))
    note: Mapped[str | None] = mapped_column(String(MAX_NOTE_LEN))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
