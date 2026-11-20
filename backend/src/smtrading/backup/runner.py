"""The PostgreSQL side of the backups: `pg_dump`, and the restore test's `pg_restore`
into the throwaway database RESTORE_DB on the same server, its sanity queries and its
drop. Tests inject a fake with the same methods (no real pg_dump)."""

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

RESTORE_DB = "smtrading_restore_test"
COUNTED_TABLES = ("portfolios", "orders", "fills")
DUMP_TIMEOUT_S = 3600
RESTORE_TIMEOUT_S = 3600
STDERR_TAIL = 400


class BackupError(Exception):
    """A step failed; the message is shown in «Salute del sistema» and on Telegram."""


class Runner(Protocol):
    async def dump(self, target: Path) -> None: ...
    async def restore(self, dump: Path, dbname: str) -> None: ...
    async def inspect(self, dbname: str) -> dict[str, Any]: ...
    async def drop(self, dbname: str) -> None: ...


def pg_args(url: URL) -> tuple[list[str], dict[str, str]]:
    """Connection argv (`-h -p -U`) and environment (PGPASSWORD, never on the argv)."""
    argv = ["--no-password", "-h", url.host or "localhost", "-p", str(url.port or 5432),
            "-U", url.username or ""]
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    if url.password:
        env["PGPASSWORD"] = url.password
    return argv, env


def _guard(dbname: str, source: str | None) -> None:
    """The restore test only ever touches RESTORE_DB, never the live database."""
    if dbname != RESTORE_DB or dbname == source:
        raise BackupError(f"database di prova non ammesso: {dbname}")


class PgRunner:
    def __init__(self, database_url: str) -> None:
        self.url = make_url(database_url)

    async def _run(self, argv: list[str], timeout: float) -> None:
        conn, env = pg_args(self.url)
        cmd = [argv[0], *conn, *argv[1:]]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise BackupError(f"{argv[0]} non disponibile: {e}") from e
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as e:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise BackupError(f"{argv[0]} oltre {int(timeout)} s") from e
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[-STDERR_TAIL:]
            raise BackupError(f"{argv[0]} uscito con {proc.returncode}: {tail}")

    async def dump(self, target: Path) -> None:
        await self._run(["pg_dump", "--format=custom", "-d", self.url.database or "",
                         "-f", str(target)], DUMP_TIMEOUT_S)

    def _engine(self, dbname: str):
        return create_async_engine(self.url.set(database=dbname), isolation_level="AUTOCOMMIT")

    async def restore(self, dump: Path, dbname: str) -> None:
        _guard(dbname, self.url.database)
        engine = self._engine("postgres")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            await engine.dispose()
        await self._run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                         "-d", dbname, str(dump)], RESTORE_TIMEOUT_S)

    async def inspect(self, dbname: str) -> dict[str, Any]:
        _guard(dbname, self.url.database)
        engine = self._engine(dbname)
        try:
            async with engine.connect() as conn:
                versions = sorted((await conn.execute(
                    text("SELECT version_num FROM alembic_version"))).scalars())
                counts = {t: int((await conn.execute(
                    text(f"SELECT count(*) FROM {t}"))).scalar_one()) for t in COUNTED_TABLES}
        finally:
            await engine.dispose()
        return {"alembic_versions": versions, "counts": counts}

    async def drop(self, dbname: str) -> None:
        _guard(dbname, self.url.database)
        engine = self._engine("postgres")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        finally:
            await engine.dispose()
