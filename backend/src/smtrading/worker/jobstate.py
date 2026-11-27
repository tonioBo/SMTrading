"""State of the worker's supervised jobs, for «Salute del sistema».

`supervise` records, per job, when it (re)started, its crashes (count, time, short error)
and, for the jobs that call `job_tick()` once per pass, the time of the last pass. The
state lives in the Redis hash `worker:jobs` (one JSON field per job, plus `__worker__`
with the worker's start time); the api reads it with `read_jobs`. The hash is emptied at
every worker start, so a renamed job does not linger.

Best effort everywhere: a Redis failure (or a test fake without `hset`) never reaches a
job, and `job_tick()` outside a supervised job does nothing."""

import json
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from smtrading.common.logsetup import redact_log_text

log = logging.getLogger(__name__)

JOBS_KEY = "worker:jobs"
WORKER_FIELD = "__worker__"
TICK_WRITE_EVERY_S = 5.0  # a fast loop writes its pass at most this often
MAX_ERROR = 200

_current: ContextVar[tuple["JobRecorder", str] | None] = ContextVar("smt_job", default=None)


class JobRecorder:
    def __init__(self, redis, *, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock
        self._jobs: dict[str, dict[str, Any]] = {}
        self._written: dict[str, float] = {}

    async def _hset(self, field: str, value: dict[str, Any]) -> None:
        try:
            await self._redis.hset(JOBS_KEY, field, json.dumps(value))
        except Exception:
            log.debug("job state write failed", exc_info=True)

    async def worker_started(self) -> None:
        try:
            await self._redis.delete(JOBS_KEY)
        except Exception:
            log.debug("job state reset failed", exc_info=True)
        await self._hset(WORKER_FIELD, {"started_at": self._clock()})

    def _job(self, name: str) -> dict[str, Any]:
        return self._jobs.setdefault(name, {
            "state": "running", "started_at": None, "last_tick_at": None, "crashes": 0,
            "last_crash_at": None, "last_error": None})

    async def _write(self, name: str) -> None:
        self._written[name] = self._clock()
        await self._hset(name, self._job(name))

    async def started(self, name: str) -> None:
        job = self._job(name)
        job.update(state="running", started_at=self._clock())
        await self._write(name)

    async def crashed(self, name: str, exc: BaseException) -> None:
        job = self._job(name)
        text = redact_log_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR]
        job.update(state="restarting", crashes=job["crashes"] + 1,
                   last_crash_at=self._clock(), last_error=text)
        await self._write(name)

    async def exited(self, name: str) -> None:
        self._job(name)["state"] = "restarting"
        await self._write(name)

    async def tick(self, name: str, info: dict[str, Any] | None = None) -> None:
        """A pass of `name`; `info` (small, JSON) replaces the job's facts shown with it."""
        now = self._clock()
        job = self._job(name)
        job["last_tick_at"] = now
        if info is not None:
            job["info"] = info
        if now - self._written.get(name, 0.0) >= TICK_WRITE_EVERY_S:
            await self._write(name)


def bind(recorder: JobRecorder | None, name: str) -> None:
    """The current task (a `supervise` loop) runs job `name`."""
    _current.set((recorder, name) if recorder is not None else None)


async def job_tick(info: dict[str, Any] | None = None) -> None:
    """One pass of the current job is done (no-op outside a supervised job)."""
    cur = _current.get()
    if cur is not None:
        await cur[0].tick(cur[1], info)


async def read_jobs(redis) -> tuple[float | None, dict[str, dict[str, Any]]]:
    """(worker start, {job: state}); Redis errors propagate."""
    raw = await redis.hgetall(JOBS_KEY)
    worker_started: float | None = None
    jobs: dict[str, dict[str, Any]] = {}
    for k, v in (raw or {}).items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        try:
            value = json.loads(v)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if key == WORKER_FIELD:
            started = value.get("started_at")
            worker_started = float(started) if isinstance(started, int | float) else None
        else:
            jobs[key] = value
    return worker_started, jobs
