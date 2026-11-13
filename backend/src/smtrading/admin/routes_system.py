"""Admin API: ledger integrity checks and database backups (pg_dump stream, backup volume)."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.engine import make_url

from smtrading.admin.auth import DbSession, require_admin
from smtrading.common.config import get_settings
from smtrading.ledger.service import verify_balances, verify_chain
from smtrading.mcp.dto import to_jsonable

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_admin)])

BACKUP_DIR = Path("/backups")  # the `backups` volume, mounted read-only on the api
BACKUP_SUFFIXES = (".sql.gz", ".dump")
_CHUNK = 64 * 1024


def dump_command() -> tuple[list[str], dict[str, str]]:
    """`pg_dump --format=custom` argv and environment, from the configured database URL."""
    url = make_url(get_settings().database_url)
    argv = [
        "pg_dump", "--format=custom", "--no-password",
        "-h", url.host or "localhost", "-p", str(url.port or 5432),
        "-U", url.username or "", "-d", url.database or "",
    ]
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    return argv, env


@router.get("/verify-chain")
async def get_verify_chain(session: DbSession) -> dict:
    report = await verify_chain(session)
    return {
        "ok": report.ok,
        "checked": report.checked,
        "first_bad_journal_id": to_jsonable(report.first_bad_journal_id),
    }


@router.get("/verify-balances")
async def get_verify_balances(session: DbSession) -> dict:
    bad = await verify_balances(session)
    return {"ok": not bad, "bad_accounts": [str(a) for a in bad]}


async def _stream(proc: asyncio.subprocess.Process, first: bytes) -> AsyncIterator[bytes]:
    # stderr is discarded from here on, but must be read so a chatty pg_dump cannot block
    drain = asyncio.create_task(proc.stderr.read())
    try:
        yield first
        while chunk := await proc.stdout.read(_CHUNK):
            yield chunk
        if await proc.wait() != 0:
            log.error("pg_dump exited with %s mid-stream", proc.returncode)
    finally:
        drain.cancel()
        if proc.returncode is None:  # client went away
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


@router.get("/backup")
async def download_backup():
    """Streams a fresh `pg_dump --format=custom`. A failure before the first byte is a 500."""
    argv, env = dump_command()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError:
        log.exception("cannot start pg_dump")
        return JSONResponse(
            {"code": "INTERNAL", "message": "pg_dump non disponibile", "details": None},
            status_code=500,
        )
    first = await proc.stdout.read(_CHUNK)
    if not first:
        stderr = (await proc.stderr.read()).decode(errors="replace")[:500]
        await proc.wait()
        log.error("pg_dump failed (%s): %s", proc.returncode, stderr)
        return JSONResponse(
            {"code": "INTERNAL", "message": "backup non riuscito", "details": None},
            status_code=500,
        )
    name = f"smtrading-{datetime.now(UTC):%Y%m%d-%H%M}.dump"
    return StreamingResponse(
        _stream(proc, first),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/backups")
async def list_backups() -> list[dict]:
    """Files of the backup volume, newest first."""
    if not BACKUP_DIR.is_dir():
        return []
    rows = []
    for p in BACKUP_DIR.iterdir():
        if p.is_file() and p.name.endswith(BACKUP_SUFFIXES):
            st = p.stat()
            rows.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            })
    return sorted(rows, key=lambda r: r["modified_at"], reverse=True)
