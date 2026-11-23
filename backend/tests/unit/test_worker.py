import asyncio
import contextlib
import logging
import signal
from types import SimpleNamespace

import pytest

from smtrading.worker import main as worker

REAL_GATE = worker.StartupGate  # the autouse fixture replaces it with an open one


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


@pytest.fixture(autouse=True)
def _idle_telegram_loops(monkeypatch):
    """The real Telegram loops (and the live monitor) would run against _FakeEngine and fail quietly under
    `supervise`: every run() test gets idle ones (a test may override them)."""
    monkeypatch.setattr(worker, "TelegramPlanner", _IdleProcessor)
    monkeypatch.setattr(worker, "OutboxSender", _IdleProcessor)
    monkeypatch.setattr(worker, "SummaryScheduler", _IdleProcessor)
    monkeypatch.setattr(worker, "LiveMonitor", _IdleProcessor)
    monkeypatch.setattr(worker, "ProtectionAlarm", _IdleProcessor)
    monkeypatch.setattr(worker, "GoalAlerts", _IdleProcessor)
    monkeypatch.setattr(worker, "PlanCloser", _IdleProcessor)
    monkeypatch.setattr(worker, "register_live_secrets", _no_live_keys)
    monkeypatch.setattr(worker, "HandoffWatchdog", _IdleProcessor)
    monkeypatch.setattr(worker, "ReconciliationJob", _IdleProcessor)
    monkeypatch.setattr(worker, "StartupGate", _OpenGate)


class _OpenGate:
    """The startup gate already open: the run() tests start every job at once."""

    def __init__(self, *args, **kwargs):
        pass

    async def wait(self):
        return None


async def _no_live_keys(session) -> int:
    return 0


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


async def test_run_supervises_telegram_planner_and_sender(monkeypatch):
    started: dict[str, asyncio.Event] = {"planner": asyncio.Event(), "sender": asyncio.Event(),
                                         "summaries": asyncio.Event()}
    seen = {}

    class FakeSummaries:
        def __init__(self, sessionmaker, **kwargs):
            seen["summaries"] = sessionmaker

        async def run(self):
            started["summaries"].set()
            await asyncio.sleep(3600)

    class FakePlanner:
        def __init__(self, sessionmaker, **kwargs):
            seen["planner"] = sessionmaker

        async def run(self):
            started["planner"].set()
            await asyncio.sleep(3600)

    class FakeSender:
        def __init__(self, sessionmaker, http, **kwargs):
            seen.update(sender=sessionmaker, http=http)

        async def run(self):
            started["sender"].set()
            await started["planner"].wait()
            await started["summaries"].wait()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    monkeypatch.setattr(worker, "JobScheduler", _IdleProcessor)
    monkeypatch.setattr(worker, "TelegramPlanner", FakePlanner)
    monkeypatch.setattr(worker, "OutboxSender", FakeSender)
    monkeypatch.setattr(worker, "SummaryScheduler", FakeSummaries)
    await asyncio.wait_for(worker.run(), 2)
    assert all(e.is_set() for e in started.values())
    assert seen["planner"] is seen["sender"] is seen["summaries"]
    assert isinstance(seen["http"], worker.httpx.AsyncClient)


async def test_run_supervises_live_monitor(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeMonitor:
        def __init__(self, sessionmaker, **kwargs):
            seen.update(sessionmaker=sessionmaker, **kwargs)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    monkeypatch.setattr(worker, "JobScheduler", _IdleProcessor)
    monkeypatch.setattr(worker, "LiveMonitor", FakeMonitor)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert isinstance(seen["qs"], worker.MarketDataRouter)


async def test_run_registers_live_keys_before_the_jobs(monkeypatch):
    """The live links' keys are registered for log masking at worker start, before any
    job (and any adapter) runs."""
    order: list[str] = []

    async def register(session):
        order.append("keys")
        return 1

    class FakeMonitor:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            order.append("job")
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(worker, "StreamManager", _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "OrderProcessor", _IdleProcessor)
    monkeypatch.setattr(worker, "AlertEvaluator", _IdleProcessor)
    monkeypatch.setattr(worker, "JobScheduler", _IdleProcessor)
    monkeypatch.setattr(worker, "LiveMonitor", FakeMonitor)
    monkeypatch.setattr(worker, "register_live_secrets", register)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *a, **kw: _SessionMaker())
    await asyncio.wait_for(worker.run(), 2)
    assert order[:2] == ["keys", "job"]


async def test_run_survives_a_live_key_registration_failure(monkeypatch, caplog):
    async def register(session):
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "register_live_secrets", register)
    with caplog.at_level(logging.WARNING, logger="smtrading.worker"):
        await worker.register_live_keys(_SessionMaker())
    assert any("live keys" in r.getMessage() for r in caplog.records)


class _SessionMaker:
    def __call__(self):
        return contextlib.nullcontext(None)


async def test_run_supervises_paperclip_reader(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeReader:
        def __init__(self, sessionmaker, **kwargs):
            seen.update(sessionmaker=sessionmaker, kwargs=kwargs)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    for name in ("StreamManager", "OrderProcessor", "AlertEvaluator", "JobScheduler"):
        monkeypatch.setattr(worker, name, _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "PaperclipReader", FakeReader)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert seen["sessionmaker"] is not None and seen["kwargs"] == {}


async def test_run_supervises_protection_alarm(monkeypatch):
    started = asyncio.Event()
    seen = {}

    class FakeAlarm:
        def __init__(self, sessionmaker, **kwargs):
            seen.update(sessionmaker=sessionmaker, kwargs=kwargs)

        async def run(self):
            started.set()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    for name in ("StreamManager", "OrderProcessor", "AlertEvaluator", "JobScheduler",
                 "PaperclipReader"):
        monkeypatch.setattr(worker, name, _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "ProtectionAlarm", FakeAlarm)
    await asyncio.wait_for(worker.run(), 2)
    assert started.is_set()
    assert seen["sessionmaker"] is not None and seen["kwargs"] == {}


# -- startup gate ------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _gate(clock, sleeps, answers, requests, **kw):
    """A real StartupGate on a fake clock; `answers` pops one item per health request:
    an exception class to raise or a status code."""

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.t += seconds

    def handler(request):
        requests.append(str(request.url))
        item = answers.pop(0)
        if isinstance(item, type):
            raise item("down", request=request)
        return worker.httpx.Response(item)

    http = worker.httpx.AsyncClient(transport=worker.httpx.MockTransport(handler))
    return REAL_GATE(http, "http://api:8000/health", clock=clock, sleep=fake_sleep,
                              **kw), http


async def test_startup_gate_waits_the_delay_then_the_api_health():
    clock, sleeps, requests = _Clock(), [], []
    answers = [worker.httpx.ConnectError, worker.httpx.ConnectError, 503]
    gate, http = _gate(clock, sleeps, answers, requests)
    clock.t += 20  # the worker started 20 s ago
    async with http:
        await gate.wait()
        assert sleeps == [worker.STARTUP_DELAY_S - 20, worker.STARTUP_POLL_S,
                          worker.STARTUP_POLL_S]
        assert worker.STARTUP_DELAY_S >= 180
        assert requests == ["http://api:8000/health"] * 3  # a 503 is an answer
        await gate.wait()  # open for good: a job restarted later does not wait again
    assert len(sleeps) == 3 and len(requests) == 3


async def test_startup_gate_opens_after_the_max_wait_without_the_api(caplog):
    clock, sleeps, requests = _Clock(), [], []
    answers = [worker.httpx.ConnectError] * 1000
    gate, http = _gate(clock, sleeps, answers, requests, max_wait_s=300, poll_s=60)
    with caplog.at_level(logging.WARNING, logger="smtrading.worker"):
        async with http:
            await gate.wait()
    assert sum(sleeps) == 300 and sleeps[0] == worker.STARTUP_DELAY_S
    assert any("api health" in r.getMessage() for r in caplog.records)


async def test_startup_gate_waiters_share_one_wait():
    clock, sleeps, requests = _Clock(), [], []
    gate, http = _gate(clock, sleeps, [200], requests)
    async with http:
        await asyncio.gather(gate.wait(), gate.wait(), gate.wait())
    assert sleeps == [worker.STARTUP_DELAY_S] and len(requests) == 1


async def test_run_holds_the_alerting_jobs_behind_the_gate(monkeypatch):
    """Protection alarm, goal alerts, plan closer, pre-open check, hand-off watchdog,
    broker reconciliation, the broker tradability refresh, the weekly PDF report, the
    market screener and the personal price alerts start only once the startup gate opens;
    the other jobs start at once."""
    waits: list[int] = []
    before: list = []
    release = asyncio.Event()
    started: list[str] = []

    class HeldGate:
        def __init__(self, http, url, **kwargs):
            assert url == worker.API_HEALTH_URL

        async def wait(self):
            waits.append(1)
            await release.wait()

    def job(name):
        class Job:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self):
                started.append(name)
                await asyncio.sleep(3600)
        return Job

    class Manager:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            for _ in range(20):
                await asyncio.sleep(0)
            before.extend([list(started), len(waits)])
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        worker, "get_settings",
        lambda: SimpleNamespace(redis_url="redis://unused:6379/0", database_url="x"),
    )
    monkeypatch.setattr(worker.Redis, "from_url", staticmethod(lambda url: _FlakyRedis()))
    monkeypatch.setattr(worker, "make_engine", lambda url: _FakeEngine())
    for name in ("OrderProcessor", "AlertEvaluator", "JobScheduler", "PaperclipReader",
                 "ClosureRefresher", "BacktestRunner", "BrokerSync"):
        monkeypatch.setattr(worker, name, _IdleProcessor)
    monkeypatch.setattr(worker, "health_checks", lambda router: asyncio.sleep(3600))
    monkeypatch.setattr(worker, "StreamManager", Manager)
    monkeypatch.setattr(worker, "StartupGate", HeldGate)
    for name in ("ProtectionAlarm", "GoalAlerts", "PlanCloser", "PreopenCheck",
                 "HandoffWatchdog", "ReconciliationJob", "WeeklyReport", "ScreenerJob",
                 "PersonalAlerts", "TradabilityRefresher"):
        monkeypatch.setattr(worker, name, job(name))
    await asyncio.wait_for(worker.run(), 2)
    assert before == [[], 10]
    assert sorted(started) == ["GoalAlerts", "HandoffWatchdog", "PersonalAlerts", "PlanCloser",
                               "PreopenCheck", "ProtectionAlarm", "ReconciliationJob",
                               "ScreenerJob", "TradabilityRefresher", "WeeklyReport"]
