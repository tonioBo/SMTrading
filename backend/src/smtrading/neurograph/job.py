"""The worker's «neurograph export» job: every CHECK_S, `run_nightly` (which runs once per
Rome night, after NIGHT_AT, and does nothing without the key)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.neurograph.sender import run_nightly
from smtrading.worker.jobstate import job_tick

log = logging.getLogger("smtrading.neurograph")

CHECK_S = 300


class NeurographExportJob:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], http: httpx.AsyncClient,
                 *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 check_s: float = CHECK_S, settings=None) -> None:
        self._sm, self._http, self._clock = sessionmaker, http, clock
        self._check_s, self._settings = check_s, settings

    async def tick(self) -> dict | None:
        try:
            return await run_nightly(self._clock(), sessionmaker=self._sm, http=self._http,
                                     settings=self._settings)
        except Exception:
            log.exception("neurograph export failed")
            return None

    async def run(self) -> None:
        while True:
            await self.tick()
            await job_tick()  # «Salute del sistema»: last pass
            await asyncio.sleep(self._check_s)
