"""Per-job state of the supervised worker jobs (worker.jobstate), for «Salute del sistema»."""

import asyncio
import json

from smtrading.worker import jobstate
from smtrading.worker import main as worker


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}

    async def delete(self, key):
        self.hashes.pop(key, None)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def test_supervise_records_start_crash_and_ticks(monkeypatch):
    monkeypatch.setattr(worker, "RESTART_DELAY_S", 0.01)
    redis, clock = FakeRedis(), Clock()
    rec = jobstate.JobRecorder(redis, clock=clock)
    await rec.worker_started()
    runs = 0
    ticked = asyncio.Event()

    async def job():
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom pcp_board_SECRET")
        clock.t = 1010.0
        await jobstate.job_tick()
        ticked.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(worker.supervise("paperclip reader", job, rec))
    await asyncio.wait_for(ticked.wait(), 2)
    task.cancel()
    started, jobs = await jobstate.read_jobs(redis)
    assert started == 1000.0
    state = jobs["paperclip reader"]
    assert state["crashes"] == 1
    assert state["last_crash_at"] == 1000.0
    assert state["last_error"].startswith("RuntimeError: boom")
    assert state["state"] == "running"  # restarted
    assert state["last_tick_at"] == 1010.0


async def test_tick_writes_are_throttled_and_tick_outside_a_job_is_a_noop():
    redis, clock = FakeRedis(), Clock()
    rec = jobstate.JobRecorder(redis, clock=clock)
    await rec.started("j")
    clock.t += 1
    await rec.tick("j")  # within TICK_WRITE_EVERY_S of the start write: kept in memory
    assert json.loads(redis.hashes[jobstate.JOBS_KEY]["j"])["last_tick_at"] is None
    clock.t += jobstate.TICK_WRITE_EVERY_S
    await rec.tick("j")
    assert json.loads(redis.hashes[jobstate.JOBS_KEY]["j"])["last_tick_at"] == clock.t
    await jobstate.job_tick()  # no supervised job in this task: nothing happens


async def test_a_redis_without_hash_commands_never_breaks_a_job():
    class Broken:
        async def set(self, *a):
            return None

    rec = jobstate.JobRecorder(Broken())
    await rec.worker_started()
    await rec.started("j")
    await rec.crashed("j", ValueError("x"))
    await rec.tick("j")


async def test_worker_start_clears_old_jobs():
    redis = FakeRedis()
    redis.hashes[jobstate.JOBS_KEY] = {"old job": json.dumps({"state": "running"})}
    await jobstate.JobRecorder(redis, clock=Clock(5.0)).worker_started()
    started, jobs = await jobstate.read_jobs(redis)
    assert (started, jobs) == (5.0, {})
