import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.corporate.snapshots import take_snapshots
from smtrading.engine.processor import OrderProcessor
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.streaming import StreamManager
from smtrading.risk.daily_loss import reset_daily
from smtrading.risk.margin import margin_sweep

log = logging.getLogger("smtrading.worker")

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_S = 10
RESTART_DELAY_S = 5
HEALTH_FIRST_DELAY_S = 30
HEALTH_INTERVAL_S = 300
MARGIN_SWEEP_INTERVAL_S = 60
SNAPSHOT_INTERVAL_S = 300


async def heartbeat(redis) -> None:
    while True:
        try:
            await redis.set(HEARTBEAT_KEY, str(time.time()))
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


async def every(name: str, interval_s: float, job: Callable[[], Awaitable[None]]) -> None:
    """Run `job()` now and then every `interval_s`; a failure is logged and retried."""
    while True:
        try:
            await job()
        except Exception:
            log.exception("%s failed; retrying in %ss", name, interval_s)
        await asyncio.sleep(interval_s)


async def risk_sweep(sessionmaker, router, redis) -> None:
    """Lift expired daily-loss blocks, then check margin and daily loss."""
    async with sessionmaker() as session:
        await reset_daily(session, datetime.now(UTC))
        await session.commit()
    await margin_sweep(sessionmaker, router, redis)


async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Run `factory()` forever: a crash is logged and the job restarted after a delay."""
    while True:
        try:
            await factory()
            log.warning("%s exited; restarting in %ss", name, RESTART_DELAY_S)
        except Exception:
            log.exception("%s crashed; restarting in %ss", name, RESTART_DELAY_S)
        await asyncio.sleep(RESTART_DELAY_S)


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
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(sessionmaker, redis, http)
            async with asyncio.TaskGroup() as tg:
                jobs = [
                    tg.create_task(heartbeat(redis)),
                    tg.create_task(
                        supervise("stream manager",
                                  lambda: StreamManager(router, redis, sessionmaker).run())
                    ),
                    tg.create_task(supervise("health checks", lambda: health_checks(router))),
                    tg.create_task(
                        supervise("order processor",
                                  lambda: OrderProcessor(sessionmaker, redis, router).run())
                    ),
                    tg.create_task(every(
                        "risk sweep", MARGIN_SWEEP_INTERVAL_S,
                        lambda: risk_sweep(sessionmaker, router, redis),
                    )),
                    tg.create_task(every(
                        "equity snapshots", SNAPSHOT_INTERVAL_S,
                        lambda: take_snapshots(sessionmaker, router),
                    )),
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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
