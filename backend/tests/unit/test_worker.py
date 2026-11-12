import asyncio
import logging
import signal
from types import SimpleNamespace

import pytest

from smtrading.worker import main as worker


class _FlakyRedis:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def set(self, key, value):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("redis down")

    async def aclose(self):
        self.closed = True


class _Stop(Exception):
    pass


async def test_heartbeat_failure_is_logged_and_loop_continues(monkeypatch, caplog):
    fake = _FlakyRedis()
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise _Stop

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.WARNING, logger="smtrading.worker"), pytest.raises(_Stop):
        await worker.heartbeat(fake)
    assert fake.calls == 2
    assert sleeps == [worker.HEARTBEAT_INTERVAL_S] * 2
    assert any("heartbeat" in r.getMessage() for r in caplog.records)


async def test_supervise_logs_crash_and_restarts(monkeypatch, caplog):
    monkeypatch.setattr(worker, "RESTART_DELAY_S", 0.01)
    runs = 0
    restarted = asyncio.Event()

    async def job():
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom")
        restarted.set()
        await asyncio.sleep(3600)

    with caplog.at_level(logging.ERROR, logger="smtrading.worker"):
        task = asyncio.create_task(worker.supervise("job", job))
        try:
            await asyncio.wait_for(restarted.wait(), 1)
        finally:
            task.cancel()
            await asyncio.wait([task])
    assert runs == 2
    assert any("crashed" in r.getMessage() for r in caplog.records)


class _FakeEngine:
    disposed = False

    async def dispose(self):
        self.disposed = True


async def test_sigterm_shuts_down_cleanly(monkeypatch):
    redis = _FlakyRedis()
    engine = _FakeEngine()
    cancelled = []

    class FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            signal.raise_signal(signal.SIGTERM)
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: redis))
    monkeypatch.setattr(worker, "make_engine", lambda url: engine)
    monkeypatch.setattr(worker, "StreamManager", FakeManager)
    await asyncio.wait_for(worker.run(), 2)
    assert cancelled == [True]
    assert redis.closed
    assert engine.disposed
