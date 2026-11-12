import asyncio
import logging
import time

from redis.asyncio import Redis

from smtrading.common.config import get_settings

log = logging.getLogger("smtrading.worker")

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_S = 10


async def run() -> None:
    redis = Redis.from_url(get_settings().redis_url)
    try:
        while True:
            try:
                await redis.set(HEARTBEAT_KEY, str(time.time()))
            except Exception:
                # Retry on the next tick: a Redis outage must not kill the worker.
                log.warning("heartbeat write failed; retrying in %ss", HEARTBEAT_INTERVAL_S,
                            exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
