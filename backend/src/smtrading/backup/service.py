"""Backup runs, restore tests, their schedule and their record.

- Backup (every day at BACKUP_AT local time, or on request from the portal): `pg_dump
  --format=custom` into the first usable destination as `<name>.partial`, its sha256,
  the config bundle (agent instructions exported for Paperclip, the compose file and
  `.env.example`; never `.env` or keys), then into every BACKUP_DIRS destination (copy to
  `.partial`, atomic rename, sidecar) and the retention there. `ok` when every
  destination has it, `parziale` when only some, `non riuscito` when none.
- Restore test (Sunday at RESTORE_AT): the newest dump whose sha256 matches its sidecar
  is restored into RESTORE_DB, which must be at the alembic head and hold portfolios,
  orders and fills; the database is dropped in every case.
- Record: `app_settings` `backup.state` (last run of each kind, last fully ok backup,
  history, files of each destination); a portal request is `backup.request`. A failed
  or partial run queues a Telegram message (category `system`)."""

import asyncio
import logging
import os
import shutil
import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.backup import files
from smtrading.backup.runner import COUNTED_TABLES, RESTORE_DB, BackupError, Runner
from smtrading.common.logsetup import redact_log_text
from smtrading.risk.models import AppSetting
from smtrading.telegram.outbox import enqueue

log = logging.getLogger(__name__)

STATE_KEY = "backup.state"
REQUEST_KEY = "backup.request"
BACKUP_AT = time(3, 30)
RESTORE_AT = time(4, 0)
RESTORE_WEEKDAY = 6  # Sunday
HISTORY = 30
POLL_S = 15.0  # the service's pass interval (a portal request waits at most this)
MAX_ERROR = 300
OK, PARTIAL, FAILED = "ok", "parziale", "non riuscito"


def _now_iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat()


def _short(e: BaseException | str) -> str:
    msg = (str(e) or type(e).__name__) if isinstance(e, BaseException) else e
    return redact_log_text(" ".join(msg.split()))[:MAX_ERROR]


@dataclass
class BackupConfig:
    dirs: list[Path]
    tz: str = "Europe/Rome"
    # the config bundle: archive folder name -> source (file or folder); missing ones skipped
    bundle_sources: dict[str, Path] = field(default_factory=dict)
    alembic_ini: Path = Path("alembic.ini")
    daily: int = files.KEEP_DAILY
    monthly: int = files.KEEP_MONTHLY

    @classmethod
    def from_env(cls, tz: str, agent_dir: str) -> "BackupConfig":
        return cls(
            dirs=files.parse_dirs(os.environ.get("BACKUP_DIRS")), tz=tz,
            bundle_sources={
                "agent-instructions": Path(agent_dir),
                "config": Path(os.environ.get("SMT_BACKUP_CONFIG_DIR", "/restore-config")),
            },
            alembic_ini=Path(os.environ.get("SMT_ALEMBIC_INI", "alembic.ini")),
        )


def alembic_heads(ini: Path) -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(ini))
    loc = cfg.get_main_option("script_location") or "migrations"
    if not Path(loc).is_absolute():
        cfg.set_main_option("script_location", str(ini.resolve().parent / loc))
    return sorted(ScriptDirectory.from_config(cfg).get_heads())


RESTORE_NOTES = """SMTrading - restore bundle ({stamp})

This archive goes with {dump} (same time stamp). It holds the agents' instructions as
exported for Paperclip (agent-instructions/) and the configuration needed to rebuild the
stack without secrets (config/: docker-compose.yml, .env.example).

NOT included, on purpose: .env and every key. The database keeps the connector and
broker credentials encrypted with SMT_MASTER_KEY: without that key (kept by the owner,
separately from these backups) they cannot be decrypted and must be entered again.

Restore steps: README of the project, section «Backup e ripristino».
"""


# -- state ------------------------------------------------------------------------------

def _empty_state() -> dict[str, Any]:
    return {"last_backup": None, "last_ok_at": None, "last_ok_name": None,
            "last_restore_test": None, "history": [], "destinations": [],
            "scheduled_backup_for": None, "scheduled_restore_for": None}


async def load_state(session: AsyncSession) -> dict[str, Any]:
    raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == STATE_KEY))
    state = _empty_state()
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in state})
    return state


async def save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    stmt = insert(AppSetting).values(key=STATE_KEY, value=state)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": state}))


async def load_request(session: AsyncSession) -> dict[str, Any] | None:
    raw = await session.scalar(select(AppSetting.value).where(AppSetting.key == REQUEST_KEY))
    return raw if isinstance(raw, dict) else None


async def request_run(session: AsyncSession, *, by: str, restore_test: bool,
                      now: datetime | None = None) -> dict[str, Any]:
    """Asks the backup service for a run now (it looks every POLL_S seconds). A request
    already waiting is kept (a restore test asked now is added to it)."""
    current = await load_request(session)
    value = current or {"requested_at": _now_iso(now or datetime.now(UTC)), "by": by,
                        "restore_test": False}
    value = {**value, "restore_test": bool(value.get("restore_test")) or restore_test}
    stmt = insert(AppSetting).values(key=REQUEST_KEY, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": value}))
    return value


def _push_history(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["history"] = ([{k: result.get(k) for k in (
        "kind", "status", "trigger", "started_at", "duration_s", "size_bytes", "name",
        "error")}] + list(state.get("history") or []))[:HISTORY]


def _alert_text(result: dict[str, Any]) -> str | None:
    if result["kind"] == "backup":
        if result["status"] == FAILED:
            return f"💾 Backup non riuscito: {result['error']}"
        if result["status"] == PARTIAL:
            bad = [d["dir"] for d in result["destinations"] if not d["ok"]]
            return (f"💾 Backup parziale: {result['name']} non copiato in "
                    f"{', '.join(bad)} ({result['error']})")
        return None
    if result["status"] != OK:
        return f"💾 Prova di ripristino del backup non riuscita: {result['error']}"
    return None


async def record(sm: async_sessionmaker[AsyncSession], result: dict[str, Any], *,
                 inventory: list[dict[str, Any]] | None = None,
                 mark: dict[str, str] | None = None, clear_request: dict | None = None
                 ) -> None:
    """Stores a run (and the destinations' files), queues the Telegram alert of a failed
    or partial one, clears the portal request it served; one transaction."""
    async with sm() as session, session.begin():
        state = await load_state(session)
        if result["kind"] == "backup":
            state["last_backup"] = result
            if result["status"] == OK:
                state["last_ok_at"], state["last_ok_name"] = result["finished_at"], result["name"]
        else:
            state["last_restore_test"] = result
        _push_history(state, result)
        if inventory is not None:
            state["destinations"] = inventory
        state.update(mark or {})
        await save_state(session, state)
        if clear_request is not None and await load_request(session) == clear_request:
            await session.execute(delete(AppSetting).where(AppSetting.key == REQUEST_KEY))
        text = _alert_text(result)
        if text:
            await enqueue(session, source="system", category="system", level="warning",
                          text=text)


# -- backup -----------------------------------------------------------------------------

def _copy_into(src: Path, dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    tmp = dest_dir / (name + files.PARTIAL_SUFFIX)
    if src.resolve() != tmp.resolve():
        shutil.copyfile(src, tmp)
    os.replace(tmp, target)
    return target


def _inventory(dirs: list[Path]) -> list[dict[str, Any]]:
    out = []
    for d in dirs:
        try:
            out.append({"dir": str(d), "files": files.inventory(d), "error": None})
        except OSError as e:
            out.append({"dir": str(d), "files": [], "error": _short(e)})
    return out


async def run_backup(sm: async_sessionmaker[AsyncSession], runner: Runner, cfg: BackupConfig,
                     *, now: datetime | None = None, trigger: str = "programmato",
                     mark: dict[str, str] | None = None, clear_request: dict | None = None
                     ) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    t0 = _time.monotonic()
    st = files.stamp(now, cfg.tz)
    name, bundle = files.dump_name(st), files.config_name(st)
    result: dict[str, Any] = {
        "kind": "backup", "trigger": trigger, "started_at": _now_iso(now), "name": name,
        "config_name": bundle, "size_bytes": None, "sha256": None, "destinations": [],
        "status": FAILED, "error": None}
    dests = {d: {"dir": str(d), "ok": False, "error": None, "deleted": []} for d in cfg.dirs}
    staging: Path | None = None
    staged: list[Path] = []
    try:
        if not cfg.dirs:
            raise BackupError("nessuna cartella di destinazione (BACKUP_DIRS vuoto)")
        for d in cfg.dirs:  # created at run time when missing
            try:
                await asyncio.to_thread(d.mkdir, parents=True, exist_ok=True)
                if staging is None:
                    staging = d
            except OSError as e:
                dests[d]["error"] = f"cartella non disponibile: {_short(e)}"
        if staging is None:
            raise BackupError("nessuna cartella di destinazione disponibile")
        tmp_dump = staging / (name + files.PARTIAL_SUFFIX)
        tmp_bundle = staging / (bundle + files.PARTIAL_SUFFIX)
        staged = [tmp_dump, tmp_bundle]
        await runner.dump(tmp_dump)
        size = (await asyncio.to_thread(tmp_dump.stat)).st_size
        if size == 0:
            raise BackupError("pg_dump ha prodotto un file vuoto")
        digest = await asyncio.to_thread(files.sha256_file, tmp_dump)
        await asyncio.to_thread(files.build_config_bundle, tmp_bundle, cfg.bundle_sources,
                                RESTORE_NOTES.format(stamp=st, dump=name))
        bundle_digest = await asyncio.to_thread(files.sha256_file, tmp_bundle)
        result.update(size_bytes=size, sha256=digest)
        # the staging folder last: its .partial files are the source of the copies
        for d in sorted(cfg.dirs, key=lambda x: x == staging):
            if dests[d]["error"]:
                continue
            try:
                for src, n, h in ((tmp_dump, name, digest), (tmp_bundle, bundle, bundle_digest)):
                    target = await asyncio.to_thread(_copy_into, src, d, n)
                    await asyncio.to_thread(files.write_sidecar, target, h)
                dests[d]["ok"] = True
                dests[d]["deleted"] = await asyncio.to_thread(
                    files.apply_retention, d, cfg.daily, cfg.monthly)
            except OSError as e:
                dests[d]["error"] = _short(e)
        oks = sum(1 for v in dests.values() if v["ok"])
        result["status"] = OK if oks == len(dests) else PARTIAL if oks else FAILED
        errors = [f"{v['dir']}: {v['error']}" for v in dests.values() if v["error"]]
        result["error"] = "; ".join(errors)[:MAX_ERROR] or None
    except Exception as e:  # recorded and alerted, never raised
        log.exception("backup failed")
        result["status"], result["error"] = FAILED, _short(e)
    finally:
        for p in staged:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                log.warning("cannot remove %s", p)
    result["destinations"] = list(dests.values())
    result["finished_at"] = _now_iso(datetime.now(UTC))
    result["duration_s"] = round(_time.monotonic() - t0, 1)
    log.info("backup %s: %s (%s bytes, %.1f s) %s", name, result["status"],
             result["size_bytes"], result["duration_s"], result["error"] or "")
    await record(sm, result, inventory=await asyncio.to_thread(_inventory, cfg.dirs),
                 mark=mark, clear_request=clear_request)
    return result


# -- restore test -----------------------------------------------------------------------

def latest_dump(dirs: list[Path]) -> Path | None:
    """The newest dump of any destination (by the time in its name)."""
    best: tuple[datetime, Path] | None = None
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            st = files.parse_stamp(p.name)
            if st and p.name.endswith(files.DUMP_SUFFIX) and p.is_file() and (
                    best is None or st > best[0]):
                best = (st, p)
    return best[1] if best else None


async def run_restore_test(sm: async_sessionmaker[AsyncSession], runner: Runner,
                           cfg: BackupConfig, *, now: datetime | None = None,
                           trigger: str = "programmato", mark: dict[str, str] | None = None,
                           clear_request: dict | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    t0 = _time.monotonic()
    result: dict[str, Any] = {"kind": "restore_test", "trigger": trigger,
                              "started_at": _now_iso(now), "name": None, "size_bytes": None,
                              "database": RESTORE_DB, "checks": {}, "status": FAILED,
                              "error": None}
    created = False
    try:
        dump = await asyncio.to_thread(latest_dump, cfg.dirs)
        if dump is None:
            raise BackupError("nessun backup da provare")
        result["name"], result["dir"] = dump.name, str(dump.parent)
        result["size_bytes"] = dump.stat().st_size
        if not await asyncio.to_thread(files.verify, dump):
            raise BackupError(f"{dump.name}: impronta sha256 mancante o diversa")
        await runner.drop(RESTORE_DB)  # a leftover of an interrupted test
        created = True
        await runner.restore(dump, RESTORE_DB)
        info = await runner.inspect(RESTORE_DB)
        heads = await asyncio.to_thread(alembic_heads, cfg.alembic_ini)
        versions = sorted(info.get("alembic_versions") or [])
        counts = info.get("counts") or {}
        result["checks"] = {"alembic_versions": versions, "alembic_heads": heads,
                            "counts": counts}
        problems = []
        if versions != heads:
            problems.append(f"versione dello schema {', '.join(versions) or 'assente'} "
                            f"invece di {', '.join(heads)}")
        problems += [f"nessuna riga in {t}" for t in COUNTED_TABLES if not counts.get(t)]
        if problems:
            raise BackupError("; ".join(problems))
        result["status"] = OK
    except Exception as e:  # recorded and alerted, never raised
        log.exception("restore test failed")
        result["status"], result["error"] = FAILED, _short(e)
    finally:
        if created:
            try:
                await runner.drop(RESTORE_DB)
            except Exception as e:
                log.exception("cannot drop %s", RESTORE_DB)
                extra = f"database di prova non eliminato: {_short(e)}"
                result["error"] = f"{result['error']}; {extra}" if result["error"] else extra
    result["finished_at"] = _now_iso(datetime.now(UTC))
    result["duration_s"] = round(_time.monotonic() - t0, 1)
    log.info("restore test %s: %s %s", result["name"], result["status"], result["error"] or "")
    await record(sm, result, mark=mark, clear_request=clear_request)
    return result


# -- schedule ---------------------------------------------------------------------------

def last_occurrence(now: datetime, tz: str, at: time, weekday: int | None = None) -> datetime:
    """The most recent scheduled instant at or before `now` (daily, or on `weekday`,
    0 = Monday), in UTC; DST aware."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    day = local.date()
    for _ in range(8):
        cand = datetime.combine(day, at, tzinfo=zone)
        if cand <= local and (weekday is None or day.weekday() == weekday):
            return cand.astimezone(UTC)
        day -= timedelta(days=1)
    raise AssertionError("unreachable")


def is_due(handled: str | None, now: datetime, tz: str, at: time,
           weekday: int | None = None) -> tuple[bool, str]:
    """(due, occurrence): due when the latest occurrence was not handled yet (a missed
    one, e.g. the PC was off, runs at the next start)."""
    occ = last_occurrence(now, tz, at, weekday).isoformat()
    return (handled is None or handled < occ), occ


async def tick(sm: async_sessionmaker[AsyncSession], runner: Runner, cfg: BackupConfig, *,
               now: datetime | None = None) -> list[str]:
    """One pass of the service: the scheduled backup, a portal request, the scheduled
    restore test. Returns what ran."""
    now = now or datetime.now(UTC)
    async with sm() as session:
        state = await load_state(session)
        req = await load_request(session)
    ran = []
    backup_due, b_occ = is_due(state.get("scheduled_backup_for"), now, cfg.tz, BACKUP_AT)
    restore_due, r_occ = is_due(state.get("scheduled_restore_for"), now, cfg.tz, RESTORE_AT,
                                RESTORE_WEEKDAY)
    if backup_due or req:
        await run_backup(sm, runner, cfg, now=now,
                         trigger="programmato" if backup_due else "manuale",
                         mark={"scheduled_backup_for": b_occ} if backup_due else None,
                         clear_request=None if (req and req.get("restore_test")) else req)
        ran.append("backup")
    if restore_due or (req and req.get("restore_test")):
        await run_restore_test(sm, runner, cfg, now=now,
                               trigger="programmato" if restore_due else "manuale",
                               mark={"scheduled_restore_for": r_occ} if restore_due else None,
                               clear_request=req if (req and req.get("restore_test")) else None)
        ran.append("restore_test")
    return ran
