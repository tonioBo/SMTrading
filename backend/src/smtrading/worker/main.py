import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.marketdata import providers  # noqa: F401  (registers the providers)
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.streaming import StreamManager

log = logging.getLogger("smtrading.worker")

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_S = 10
RESTART_DELAY_S = 5


async def heartbeat(redis) -> None:
    while True:
        try:
            await redis.set(HEARTBEAT_KEY, str(time.time()))
        except Exception:
            # Retry on the next tick: a Redis outage must not kill the worker.
            log.warning("heartbeat write failed; retrying in %ss", HEARTBEAT_INTERVAL_S,
                        exc_info=True)
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


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
