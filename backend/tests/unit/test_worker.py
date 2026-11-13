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


async def test_supervise_stops_when_cancelled_during_a_crash(monkeypatch):
    """A job that turns its cancellation into an error (a TaskGroup whose child failed)
    must not swallow the cancel: supervise ends instead of restarting."""
    monkeypatch.setattr(worker, "RESTART_DELAY_S", 3600)
    started = asyncio.Event()

    async def job():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    task = asyncio.create_task(worker.supervise("job", job))
    await started.wait()
    task.cancel()
    done, _ = await asyncio.wait([task], timeout=1)
    assert task in done and task.cancelled()


class _IdleProcessor:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self):
        await asyncio.sleep(3600)


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
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    await asyncio.wait_for(worker.run(), 2)
    assert cancelled == [True]
    assert redis.closed
    assert engine.disposed


async def test_health_checks_first_after_30s_then_every_300s(monkeypatch, caplog):
    sleeps: list[float] = []
    checks: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise _Stop

    class Router:
        async def check_health(self):
            checks.append(len(sleeps))
            if len(checks) == 2:
                raise RuntimeError("db down")
            return {"yahoo": True}

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.WARNING, logger="smtrading.worker"), pytest.raises(_Stop):
        await worker.health_checks(Router())
    assert sleeps == [30, 300, 300, 300]
    assert checks == [1, 2, 3]  # one check after each wait; a failure does not stop the loop
    assert any("health check failed" in r.getMessage() for r in caplog.records)


async def test_run_supervises_health_checks(monkeypatch):
    started = asyncio.Event()

    async def fake_health(router):
        assert router is not None
        started.set()
        await asyncio.sleep(3600)

    class FakeManager:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            await started.wait()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", FakeManager)
    monkeypatch.setattr(worker, "health_checks", fake_health)
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()


async def test_run_supervises_order_processor(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeProcessor:
        def __init__(self, sessionmaker, redis, qs, **kwargs):
            seen.update(sessionmaker=sessionmaker, redis=redis, qs=qs)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    redis = _FlakyRedis()
    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: redis))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", FakeProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert seen["redis"] is redis
    assert isinstance(seen["qs"], worker.MarketDataRouter)


async def test_run_supervises_job_scheduler(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeScheduler:
        def __init__(self, sessionmaker, qs, redis, **kwargs):
            seen.update(sessionmaker=sessionmaker, qs=qs, redis=redis)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    redis = _FlakyRedis()
    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: redis))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    monkeypatch.setattr(worker, "JobScheduler", FakeScheduler)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert seen["redis"] is redis
    assert isinstance(seen["qs"], worker.MarketDataRouter)


async def test_run_supervises_alert_evaluator(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeEvaluator:
        def __init__(self, sessionmaker, redis, qs, **kwargs):
            seen.update(sessionmaker=sessionmaker, redis=redis, qs=qs)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    redis = _FlakyRedis()
    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: redis))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "JobScheduler", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", FakeEvaluator)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert seen["redis"] is redis
    assert isinstance(seen["qs"], worker.MarketDataRouter)
