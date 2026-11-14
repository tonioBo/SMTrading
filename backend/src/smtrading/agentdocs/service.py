"""«Istruzioni agenti»: seed from docs/paperclip-agents, read, save (optimistic on
`base_version`) and restore. Nothing here commits."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agentdocs.models import MAX_BODY_BYTES, MAX_NOTE_LEN, AgentDoc, AgentDocVersion
from smtrading.common.errors import ErrorCode, SMTError

log = logging.getLogger(__name__)

SEED_AUTHOR = "seed"
# docs/paperclip-agents in a checkout; the image sets SMT_AGENT_DOCS_DIR (see Dockerfile)
DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[4] / "docs" / "paperclip-agents"
CONFLICT_MESSAGE = "Il testo è stato modificato da qualcun altro, ricarica"

_FIXED = {"README.md": ("readme", "Guida all'installazione"),
          "00-regole-comuni.md": ("common", "Regole comuni")}
_ROLE_FILE = re.compile(r"^\d{2}-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
_HEADING = re.compile(r"^#\s+(?:\d{2}\s*·\s*)?(.+?)\s*$", re.MULTILINE)


class VersionConflict(Exception):
    def __init__(self, current_version: int) -> None:
        super().__init__(CONFLICT_MESSAGE)
        self.current_version = current_version


def doc_identity(filename: str, body: str) -> tuple[str, str] | None:
    """(slug, title) of a docs file: README.md → readme, 00-regole-comuni.md → common,
    NN-<slug>.md → <slug> titled by its first heading (without "NN · "); else None."""
    if filename in _FIXED:
        return _FIXED[filename]
    if (m := _ROLE_FILE.match(filename)) is None:
        return None
    heading = _HEADING.search(body)
    slug = m.group(1)
    return slug, (heading.group(1) if heading else slug)[:200]


async def seed_agent_docs(session: AsyncSession, docs_dir: Path) -> int:
    """Insert-only: a document already in the database (edited or not) is never touched.
    Returns how many documents were added (0 when the folder is missing)."""
    if not docs_dir.is_dir():
        log.warning("agent docs folder %s missing: «Istruzioni agenti» not seeded", docs_dir)
        return 0
    added = 0
    for path in sorted(docs_dir.glob("*.md")):
        body = path.read_text("utf-8")
        identity = doc_identity(path.name, body)
        if identity is None:
            continue
        slug, title = identity
        inserted = await session.scalar(
            insert(AgentDoc).values(slug=slug, title=title, current_version=1)
            .on_conflict_do_nothing().returning(AgentDoc.slug)
        )
        if inserted is None:
            continue
        session.add(AgentDocVersion(slug=slug, version=1, body=body, author=SEED_AUTHOR))
        added += 1
    await session.flush()
    return added


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _not_found(what: str) -> SMTError:
    return SMTError(ErrorCode.NOT_FOUND, f"{what} non trovato")


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


async def _current(session: AsyncSession, slugs: list[str] | None = None):
    q = (select(AgentDoc, AgentDocVersion)
         .join(AgentDocVersion, (AgentDocVersion.slug == AgentDoc.slug)
               & (AgentDocVersion.version == AgentDoc.current_version))
         .order_by(AgentDoc.slug))
    if slugs is not None:
        q = q.where(AgentDoc.slug.in_(slugs))
    return (await session.execute(q)).all()


def _summary(doc: AgentDoc, current: AgentDocVersion) -> dict[str, Any]:
    return {"slug": doc.slug, "title": doc.title, "current_version": doc.current_version,
            "updated_at": _iso(current.created_at), "updated_by": current.author}


async def list_docs(session: AsyncSession) -> list[dict[str, Any]]:
    return [_summary(d, v) for d, v in await _current(session)]


async def get_doc(session: AsyncSession, slug: str) -> dict[str, Any]:
    rows = await _current(session, [slug])
    if not rows:
        raise _not_found("documento")
    doc, current = rows[0]
    return {**_summary(doc, current), "body": current.body}


async def list_versions(session: AsyncSession, slug: str) -> list[dict[str, Any]]:
    await _locked_doc(session, slug, lock=False)
    rows = await session.scalars(
        select(AgentDocVersion).where(AgentDocVersion.slug == slug)
        .order_by(AgentDocVersion.version.desc())
    )
    return [{"version": v.version, "author": v.author, "note": v.note,
             "created_at": _iso(v.created_at), "size": len(v.body.encode("utf-8"))}
            for v in rows]


async def _version(session: AsyncSession, slug: str, version: int) -> AgentDocVersion:
    row = await session.scalar(
        select(AgentDocVersion).where(AgentDocVersion.slug == slug,
                                      AgentDocVersion.version == version)
    )
    if row is None:
        raise _not_found("versione")
    return row


async def get_version(session: AsyncSession, slug: str, version: int) -> dict[str, Any]:
    await _locked_doc(session, slug, lock=False)
    v = await _version(session, slug, version)
    return {"slug": slug, "version": v.version, "body": v.body, "author": v.author,
            "note": v.note, "created_at": _iso(v.created_at)}


async def _locked_doc(session: AsyncSession, slug: str, *, lock: bool = True) -> AgentDoc:
    q = select(AgentDoc).where(AgentDoc.slug == slug)
    doc = await session.scalar(q.with_for_update() if lock else q)
    if doc is None:
        raise _not_found("documento")
    return doc


def clean_note(note: str | None) -> str | None:
    return (note or "").strip() or None


def _validate(body: str, note: str | None) -> str | None:
    if not body.strip():
        raise _invalid("body", "Il testo non può essere vuoto")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise _invalid("body", f"Il testo supera {MAX_BODY_BYTES // 1024} kB")
    note = clean_note(note)
    if note is not None and len(note) > MAX_NOTE_LEN:
        raise _invalid("note", f"La nota supera {MAX_NOTE_LEN} caratteri")
    return note


async def _append(session: AsyncSession, doc: AgentDoc, body: str, note: str | None,
                  author: str) -> None:
    doc.current_version += 1
    session.add(AgentDocVersion(slug=doc.slug, version=doc.current_version, body=body,
                                author=author, note=note))
    await session.flush()


async def save_doc(
    session: AsyncSession, slug: str, *, body: str, note: str | None, base_version: int,
    author: str,
) -> tuple[dict[str, Any], bool]:
    """A new version with `body`, if `base_version` is still the current one (else
    VersionConflict). The same text as the current version adds nothing: changed=False."""
    note = _validate(body, note)
    doc = await _locked_doc(session, slug)
    if base_version != doc.current_version:
        raise VersionConflict(doc.current_version)
    current = await _version(session, slug, doc.current_version)
    if current.body == body:
        return await get_doc(session, slug), False
    await _append(session, doc, body, note, author)
    return await get_doc(session, slug), True


async def restore_version(
    session: AsyncSession, slug: str, version: int, *, author: str
) -> dict[str, Any]:
    """A new version copying `version` (which must not be the current one)."""
    doc = await _locked_doc(session, slug)
    old = await _version(session, slug, version)
    if version == doc.current_version:
        raise _invalid("version", "È già la versione corrente")
    await _append(session, doc, old.body, f"Ripristino della versione {version}", author)
    return await get_doc(session, slug)
