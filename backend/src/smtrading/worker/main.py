import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

import smtrading.models  # noqa: F401  (complete mapper registry before any session)
from smtrading.backtest.runner import BacktestRunner
from smtrading.brokers.live.account import register_live_secrets
from smtrading.brokers.live.monitor import LiveMonitor
from smtrading.brokers.live.startup import warn_if_fake_broker
from smtrading.brokers.sync import BrokerSync
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.common.heartbeat import write_heartbeat
from smtrading.common.logsetup import configure_logging
from smtrading.engine.processor import OrderProcessor
from smtrading.instruments.closures import ClosureRefresher
from smtrading.jobs.scheduler import JobScheduler
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.streaming import StreamManager
from smtrading.telegram.planner import TelegramPlanner
from smtrading.telegram.sender import OutboxSender
from smtrading.telegram.summaries import SummaryScheduler
from smtrading.watch.evaluator import AlertEvaluator

log = logging.getLogger("smtrading.worker")

HEARTBEAT_INTERVAL_S = 10
RESTART_DELAY_S = 5
HEALTH_FIRST_DELAY_S = 30
HEALTH_INTERVAL_S = 300


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
        except Exception:
            log.warning("connector health check failed; retrying in %ss", HEALTH_INTERVAL_S,
                        exc_info=True)
        await asyncio.sleep(HEALTH_INTERVAL_S)


async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Run `factory()` forever: a crash is logged and the job restarted after a delay.

    A cancel that the job turned into an error (e.g. a TaskGroup whose child failed while
    it was being cancelled) still stops the loop instead of being swallowed.
    """
    task = asyncio.current_task()
    while True:
        try:
            await factory()
            log.warning("%s exited; restarting in %ss", name, RESTART_DELAY_S)
        except Exception:
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from None
            log.exception("%s crashed; restarting in %ss", name, RESTART_DELAY_S)
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
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    engine = make_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await register_live_keys(sessionmaker)
        await warn_if_fake_broker(sessionmaker, process="worker")
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(sessionmaker, redis, http)
            async with asyncio.TaskGroup() as tg:
                jobs = [
                    tg.create_task(heartbeat(redis)),
                    tg.create_task(
                        supervise("market closures", lambda: ClosureRefresher(sessionmaker).run())
                    ),
                    tg.create_task(
                        supervise("stream manager",
                                  lambda: StreamManager(router, redis, sessionmaker).run())
                    ),
                    tg.create_task(supervise("health checks", lambda: health_checks(router))),
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
                        supervise("telegram planner", lambda: TelegramPlanner(sessionmaker).run())
                    ),
                    tg.create_task(
                        supervise("telegram sender",
                                  lambda: OutboxSender(sessionmaker, http).run())
                    ),
                    tg.create_task(supervise("telegram summaries",
                                             lambda: SummaryScheduler(sessionmaker).run())),
                ]
                await stop.wait()
                log.info("shutdown requested")
                for job in jobs:
                    job.cancel()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(logging.INFO)
    asyncio.run(run())
