"""`python -m smtrading.autorules.main`: the `scalper` service (intraday mode B).

The worker's settings, logging and startup gate, and one supervised job, the Scalper. It
opens no price stream and runs no other job: the worker keeps the stream manager, the
order processor and BrokerSync (the only writer to the brokers). Its job state goes to
the worker's «Salute del sistema» hash under «scalper», never resetting it."""
import asyncio
import contextlib
import logging
import signal

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

import smtrading.models  # noqa: F401  (complete mapper registry before any session)
from smtrading.autorules.service import SCALPER_JOB, Scalper
from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.common.logsetup import configure_logging
from smtrading.marketdata.router import MarketDataRouter
from smtrading.worker.jobstate import JobRecorder
from smtrading.worker.main import (
    API_HEALTH_URL,
    StartupGate,
    gated,
    register_live_keys,
    supervise,
)

log = logging.getLogger("smtrading.scalper")


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
        recorder = JobRecorder(redis)  # never worker_started(): the hash is the worker's
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(sessionmaker, redis, http)
            gate = StartupGate(http, getattr(settings, "api_health_url", API_HEALTH_URL))
            job = asyncio.create_task(supervise(SCALPER_JOB, lambda: gated(
                gate, lambda: Scalper(sessionmaker, redis, router).run()), recorder))
            await stop.wait()
            log.info("shutdown requested")
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(logging.INFO)
    asyncio.run(run())
