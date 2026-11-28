import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

import smtrading.models  # noqa: F401  (complete mapper registry before any session)
from smtrading.autorules.ownership import owned_symbols
from smtrading.backtest.research_runner import ResearchRunner
from smtrading.backtest.runner import BacktestRunner
from smtrading.brokers.live.account import register_live_secrets
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.monitor import LiveMonitor
from smtrading.brokers.live.startup import warn_if_fake_broker
from smtrading.brokers.sync import BrokerSync
from smtrading.brokers.tradability import TradabilityRefresher
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.common.heartbeat import write_heartbeat
from smtrading.common.logsetup import configure_logging
from smtrading.engine.processor import OrderProcessor
from smtrading.goals.alerts import GoalAlerts
from smtrading.goals.plan_closer import PlanCloser
from smtrading.instruments.closures import ClosureRefresher
from smtrading.intraday.job import IntradayJob
from smtrading.jobs.scheduler import JobScheduler
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.streaming import StreamManager
from smtrading.marketdata.suspect_alarm import SuspectAlarm
from smtrading.marketsportal.alerts import PersonalAlerts
from smtrading.marketsportal.overview import OverviewService
from smtrading.marketsportal.screener import ScreenerJob
from smtrading.marketsportal.yahoo import PortalYahoo
from smtrading.neurograph.job import NeurographExportJob
from smtrading.paperclip.reader import PaperclipReader
from smtrading.paperclip.watchdog import HandoffWatchdog
from smtrading.positions.protection import ProtectionAlarm
from smtrading.reconciliation.job import ReconciliationJob
from smtrading.sentinel import errorlog
from smtrading.sentinel.job import SystemSentinel
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.preopen import PreopenCheck
from smtrading.telegram.sender import OutboxSender
from smtrading.telegram.summaries import SummaryScheduler
from smtrading.telegram.weekly_report import WeeklyReport
from smtrading.watch.evaluator import AlertEvaluator
from smtrading.worker.jobstate import JobRecorder, bind, job_tick

log = logging.getLogger("smtrading.worker")

HEARTBEAT_INTERVAL_S = 10
RESTART_DELAY_S = 5
HEALTH_FIRST_DELAY_S = 30
HEALTH_INTERVAL_S = 300
# the alerting jobs (protection alarm, goal alerts, plan closer, pre-open check, hand-off
# watchdog, broker reconciliation, weekly PDF report, system sentinel, intraday, the
# nightly export to Neurograph) wait
# STARTUP_DELAY_S after the worker start and until the api health endpoint answers, so a
# deploy restart fires nothing; after STARTUP_MAX_WAIT_S they start anyway (fail open)
STARTUP_DELAY_S = 180
STARTUP_POLL_S = 10
STARTUP_MAX_WAIT_S = 900
STARTUP_HEALTH_TIMEOUT_S = 5
API_HEALTH_URL = "http://api:8000/health"


async def heartbeat(redis) -> None:
    while True:
        try:
            await write_heartbeat(redis)
        except Exception:
            # Retry on the next tick: a Redis outage must not kill the worker.
            log.warning("heartbeat write failed; retrying in %ss", HEARTBEAT_INTERVAL_S,
                        exc_info=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def health_checks(router) -> None:
    """Health-check every enabled connector: first after 30s, then every 300s."""
    await asyncio.sleep(HEALTH_FIRST_DELAY_S)
    while True:
        try:
            results = await router.check_health()
            log.info("connector health: %s", results)
            await job_tick()
        except Exception:
            log.warning("connector health check failed; retrying in %ss", HEALTH_INTERVAL_S,
                        exc_info=True)
        await asyncio.sleep(HEALTH_INTERVAL_S)


class StartupGate:
    """Opens once, for every job behind it: STARTUP_DELAY_S after its creation (the worker
    start) and once `url` answers (any HTTP status: the api is up; a 503 only says it is
    degraded). A job restarted by `supervise` later does not wait again. Never past
    `max_wait_s`: the alarms protect money, they are held back, never silenced."""

    def __init__(self, http: httpx.AsyncClient, url: str, *, delay_s: float = STARTUP_DELAY_S,
                 poll_s: float = STARTUP_POLL_S, max_wait_s: float = STARTUP_MAX_WAIT_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._http, self._url = http, url
        self._delay_s, self._poll_s, self._max_wait_s = delay_s, poll_s, max_wait_s
        self._clock, self._sleep = clock, sleep
        self._started = clock()
        self._open = False
        self._lock = asyncio.Lock()

    def _elapsed(self) -> float:
        return self._clock() - self._started

    async def _api_answers(self) -> bool:
        try:
            await self._http.get(self._url, timeout=STARTUP_HEALTH_TIMEOUT_S)
        except httpx.HTTPError:
            return False
        return True

    async def wait(self) -> None:
        if self._open:
            return
        async with self._lock:
            if self._open:
                return
            if (remaining := self._delay_s - self._elapsed()) > 0:
                await self._sleep(remaining)
            while not await self._api_answers():
                if self._elapsed() >= self._max_wait_s:
                    log.warning("api health not answering after %ss: alerting jobs start "
                                "anyway", self._max_wait_s)
                    break
                await self._sleep(min(self._poll_s,
                                      max(self._max_wait_s - self._elapsed(), 0)))
            self._open = True
            log.info("startup gate open: alerting jobs start")


async def gated(gate: StartupGate, factory: Callable[[], Awaitable[None]]) -> None:
    await gate.wait()
    await factory()


# set by run(): the default recorder of `supervise`
_recorder: JobRecorder | None = None


async def supervise(name: str, factory: Callable[[], Awaitable[None]],
                    recorder: JobRecorder | None = None) -> None:
    """Run `factory()` forever: a crash is logged and the job restarted after a delay.

    A cancel that the job turned into an error (e.g. a TaskGroup whose child failed while
    it was being cancelled) still stops the loop instead of being swallowed. With a
    `recorder` every start, crash and exit is recorded (worker.jobstate), and the job's
    own `job_tick()` calls record its passes (default: the worker's recorder, set by run()).
    """
    recorder = recorder or _recorder
    task = asyncio.current_task()
    bind(recorder, name)
    while True:
        try:
            if recorder is not None:
                await recorder.started(name)
            await factory()
            log.warning("%s exited; restarting in %ss", name, RESTART_DELAY_S)
            if recorder is not None:
                await recorder.exited(name)
        except Exception as e:
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            log.exception("%s crashed; restarting in %ss", name, RESTART_DELAY_S)
            if recorder is not None:
                await recorder.crashed(name, e)
        await asyncio.sleep(RESTART_DELAY_S)


async def register_live_keys(sessionmaker) -> None:
    """The live broker keys are masked in the worker's logs from the start (every
    decrypt registers them again); best-effort: a database not reachable yet does not
    stop the worker."""
    try:
        async with sessionmaker() as session:
            count = await register_live_secrets(session)
        if count:
            log.info("live keys registered for log masking: %s link(s)", count)
    except Exception:
        log.warning("live keys not registered at start", exc_info=True)


async def run() -> None:
    global _recorder
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    engine = make_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    # ERROR/CRITICAL records per logger, for the system sentinel (flushed to Redis)
    error_counter = errorlog.install("worker")
    try:
        await register_live_keys(sessionmaker)
        await warn_if_fake_broker(sessionmaker, process="worker")
        _recorder = JobRecorder(redis)  # every supervised job's state («Salute del sistema»)
        await _recorder.worker_started()
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(sessionmaker, redis, http)
            gate = StartupGate(http, getattr(settings, "api_health_url", API_HEALTH_URL))
            async with asyncio.TaskGroup() as tg:
                jobs = [
                    tg.create_task(heartbeat(redis)),
                    tg.create_task(errorlog.flusher(redis, error_counter)),
                    tg.create_task(
                        supervise("market closures", lambda: ClosureRefresher(sessionmaker).run())
                    ),
                    tg.create_task(
                        supervise("stream manager",
                                  lambda: StreamManager(router, redis, sessionmaker).run())
                    ),
                    tg.create_task(supervise("health checks", lambda: health_checks(router))),
                    tg.create_task(supervise("price quality alarm",
                                             lambda: SuspectAlarm(sessionmaker, redis).run())),
                    tg.create_task(
                        supervise("order processor",
                                  lambda: OrderProcessor(sessionmaker, redis, router).run())
                    ),
                    tg.create_task(
                        supervise("alert evaluator",
                                  lambda: AlertEvaluator(sessionmaker, redis, router).run())
                    ),
                    tg.create_task(
                        supervise("backtest runner", lambda: BacktestRunner(sessionmaker, router).run())
                    ),
                    tg.create_task(
                        supervise("research runner",
                                  lambda: ResearchRunner(sessionmaker, router).run())
                    ),
                    tg.create_task(
                        supervise("broker sync",
                                  lambda: BrokerSync(sessionmaker, redis, router, http).run())
                    ),
                    tg.create_task(
                        supervise("live monitor",
                                  lambda: LiveMonitor(sessionmaker, qs=router).run())
                    ),
                    tg.create_task(
                        supervise("job scheduler",
                                  lambda: JobScheduler(sessionmaker, router, redis,
                                                       http=http).run())
                    ),
                    tg.create_task(
                        supervise("telegram planner", lambda: TelegramPlanner(sessionmaker, qs=router).run())
                    ),
                    tg.create_task(
                        supervise("telegram sender",
                                  lambda: OutboxSender(sessionmaker, http).run())
                    ),
                    tg.create_task(supervise("telegram summaries",
                                             lambda: SummaryScheduler(sessionmaker, qs=router,
                                                                      redis=redis).run())),
                    tg.create_task(supervise("paperclip reader",
                                             lambda: PaperclipReader(sessionmaker).run())),
                    tg.create_task(supervise("protection alarm", lambda: gated(
                        gate, lambda: ProtectionAlarm(sessionmaker).run()))),
                    tg.create_task(supervise("goal alerts", lambda: gated(
                        gate, lambda: GoalAlerts(sessionmaker, router).run()))),
                    tg.create_task(supervise("plan closer", lambda: gated(
                        gate, lambda: PlanCloser(sessionmaker, router, redis).run()))),
                    tg.create_task(supervise("pre-open check", lambda: gated(
                        gate, lambda: PreopenCheck(sessionmaker, router, redis).run()))),
                    tg.create_task(supervise("hand-off watchdog", lambda: gated(
                        gate, lambda: HandoffWatchdog(sessionmaker).run()))),
                    tg.create_task(supervise("market screener", lambda: gated(
                        gate, lambda: ScreenerJob(sessionmaker, router, redis).run()))),
                    tg.create_task(supervise("personal price alerts", lambda: gated(
                        gate, lambda: PersonalAlerts(
                            sessionmaker, router, redis, OverviewService(
                                redis, PortalYahoo(redis, sessionmaker))).run()))),
                    tg.create_task(supervise("weekly report", lambda: gated(
                        gate, lambda: WeeklyReport(sessionmaker, router).run()))),
                    tg.create_task(supervise("system sentinel", lambda: gated(
                        gate, lambda: SystemSentinel(sessionmaker, redis).run()))),
                    tg.create_task(supervise("broker tradability", lambda: gated(
                        gate, lambda: TradabilityRefresher(sessionmaker, redis, http).run()))),
                    tg.create_task(supervise("intraday", lambda: gated(
                        gate, lambda: IntradayJob(
                            sessionmaker, router, redis, http,
                            exclude_provider=owned_symbols).run()))),
                    tg.create_task(supervise("neurograph export", lambda: gated(
                        gate, lambda: NeurographExportJob(sessionmaker, http).run()))),
                    tg.create_task(supervise("broker reconciliation", lambda: gated(
                        gate, lambda: ReconciliationJob(
                            sessionmaker, router, http,
                            live_http_factory=lambda link_id: make_live_client(
                                sessionmaker, link_id)).run()))),
                ]
                await stop.wait()
                log.info("shutdown requested")
                for job in jobs:
                    job.cancel()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        _recorder = None
        errorlog.uninstall(error_counter)
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(logging.INFO)
    asyncio.run(run())
