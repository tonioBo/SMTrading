import asyncio
import time

from redis.asyncio import Redis

from smtrading.common.config import get_settings

HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_INTERVAL_S = 10


async def run() -> None:
    redis = Redis.from_url(get_settings().redis_url)
    try:
        while True:
            await redis.set(HEARTBEAT_KEY, str(time.time()))
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
