"""The compose `backup` service: `python -m smtrading.backup.main`.

Every POLL_S seconds one `service.tick`: the nightly backup (03:30 local time), a backup
asked from the portal (POST /api/backups/run) and the Sunday 04:00 restore test. A
missed time (the PC was off) runs at the next start. Destinations: BACKUP_DIRS (paths
separated by `;`), created when missing."""

import asyncio
import contextlib
import logging
import signal

import smtrading.models  # noqa: F401  (complete mapper registry before any session)
from smtrading.backup.runner import PgRunner
from smtrading.backup.service import POLL_S, BackupConfig, tick
from smtrading.common.config import get_settings
from smtrading.common.db import get_sessionmaker
from smtrading.common.logsetup import configure_logging

log = logging.getLogger("smtrading.backup")


async def run() -> None:
    settings = get_settings()
    cfg = BackupConfig.from_env(settings.tz, settings.agent_docs_export_dir)
    runner = PgRunner(settings.database_url)
    sm = get_sessionmaker()
    log.info("backup service started: %d destinations (%s)", len(cfg.dirs),
             "; ".join(str(d) for d in cfg.dirs) or "none: set BACKUP_DIRS")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        try:
            await tick(sm, runner, cfg)
        except Exception:  # e.g. the database restarting: next pass
            log.exception("backup pass failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), POLL_S)
    log.info("backup service stopped")


if __name__ == "__main__":
    configure_logging(logging.INFO)
    asyncio.run(run())
