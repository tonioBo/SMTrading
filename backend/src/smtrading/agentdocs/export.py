"""Export of the «Istruzioni agenti» for Paperclip's "External" instructions mode.

For each agent slug: `<dir>/<slug>/AGENTS.md` = what «Copia istruzioni complete» produces
(common + "---" + role), the file Paperclip loads at every run; next to it `APPENDICE.md` =
the appendix (slug `appendice`, the rare cases), which Paperclip does not load: the agents
read its sections on demand (`get_instructions_appendix`, or this file). Files are written
atomically and only when the content changed.
A missing/unwritable folder is a warning (logged once) and a status error: it never fails
the caller (a document save)."""

import asyncio
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.agentdocs import service
from smtrading.agentdocs.appendix import APPENDIX_FILE, APPENDIX_SLUG
from smtrading.common.config import get_settings

log = logging.getLogger(__name__)

NON_AGENT_SLUGS = ("readme", "common", APPENDIX_SLUG)
PAPERCLIP_ROOT = "/smtrading-instructions"
ENTRY_FILE = "AGENTS.md"
README_TEXT = (
    "Questi file sono generati automaticamente da SMTrading (per ogni agente, in "
    "<agente>/AGENTS.md: regole comuni + ruolo; in <agente>/APPENDICE.md: l'appendice dei "
    "casi rari). Non modificarli qui: verrebbero "
    "sovrascritti. Per cambiare le istruzioni usa la pagina «Istruzioni agenti» del portale; "
    "ogni salvataggio aggiorna questi file.\n"
)

_status: dict[str, Any] = {"last_export_at": None, "ok": None, "error": None, "exported": 0,
                           "dir": None}
_warned = False


def full_instructions(common: str, role: str) -> str:
    """Same composition as «Copia istruzioni complete» in the web app."""
    return f"{common.strip()}\n\n---\n\n{role.strip()}\n"


def export_dir() -> str:
    return get_settings().agent_docs_export_dir


def paperclip_path(slug: str) -> str:
    return f"{PAPERCLIP_ROOT}/{slug}"


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Bind mounts from a Windows drive (Docker Desktop / WSL drvfs) refuse chmod for a
    non-owner (EPERM) while still allowing writes: permissions are cosmetic there."""
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def _write_if_changed(path: Path, content: str) -> bool:
    data = content.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(mode=0o755, exist_ok=True)
    _chmod_best_effort(path.parent, 0o755)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _chmod_best_effort(Path(tmp), 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _write_all(root: Path, files: dict[str, str], appendix: str | None) -> int:
    if not root.is_dir():
        raise FileNotFoundError(f"la cartella {root} non esiste")
    _write_if_changed(root / "README.txt", README_TEXT)
    for slug, content in files.items():
        _write_if_changed(root / slug / ENTRY_FILE, content)
        if appendix is not None:
            _write_if_changed(root / slug / APPENDIX_FILE, appendix)
    return len(files)


async def _bodies(session: AsyncSession) -> dict[str, str]:
    return {d.slug: v.body for d, v in await service._current(session)}


def _agent_files(bodies: dict[str, str]) -> dict[str, str]:
    common = bodies.get("common", "")
    return {slug: full_instructions(common, body) for slug, body in bodies.items()
            if slug not in NON_AGENT_SLUGS}


async def agent_files(session: AsyncSession) -> dict[str, str]:
    return _agent_files(await _bodies(session))


async def export_all(session: AsyncSession) -> dict[str, Any]:
    """Export every agent; never raises. Returns {exported, dir, ok, error?}."""
    global _warned
    root = export_dir()
    exported, error = 0, None
    try:
        bodies = await _bodies(session)
        exported = await asyncio.to_thread(_write_all, Path(root), _agent_files(bodies),
                                           bodies.get(APPENDIX_SLUG))
    except Exception as exc:  # noqa: BLE001 - an export problem must not fail a save
        error = f"Esportazione non riuscita: {exc}"
        if not _warned:
            log.warning("agent docs export to %s failed: %s", root, exc)
            _warned = True
    else:
        _warned = False
    _status.update(last_export_at=datetime.now(UTC).isoformat(), ok=error is None,
                   error=error, exported=exported, dir=root)
    out: dict[str, Any] = {"exported": exported, "dir": root, "ok": error is None}
    if error:
        out["error"] = error
    return out


async def export_status(session: AsyncSession) -> dict[str, Any]:
    slugs = sorted(s for s in (await agent_files(session)))
    return {"last_export_at": _status["last_export_at"], "ok": _status["ok"],
            "error": _status["error"], "dir": export_dir(),
            "entry_file": ENTRY_FILE,
            "agents": [{"slug": s, "path": paperclip_path(s)} for s in slugs]}


def reset_state() -> None:
    global _warned
    _warned = False
    _status.update(last_export_at=None, ok=None, error=None, exported=0, dir=None)
