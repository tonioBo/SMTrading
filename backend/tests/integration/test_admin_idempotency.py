"""The in-flight marker of an Idempotency-Key request (admin.idempotency)."""

import asyncio
import uuid

from redis.asyncio import Redis

from smtrading.admin.idempotency import RESET_IN_FLIGHT_TTL_S, Idempotency


async def test_in_flight_marker_is_refreshed_while_running(redis_url):
    redis = Redis.from_url(redis_url)
    key = uuid.uuid4().hex
    lock_key = f"idem:reset:{key}:lock"
    idem = Idempotency(redis, "reset", key, "fp", in_flight_ttl_s=3)
    seen: list[bool] = []

    async def slow() -> dict:
        await asyncio.sleep(4)  # longer than the TTL: only the refresh keeps the marker
        seen.append(await redis.exists(lock_key) == 1)
        return {"ok": True}

    try:
        response = await idem.run(slow)
        assert response.status_code == 200
        assert seen == [True]
        assert await redis.exists(lock_key) == 0  # released by its owner
    finally:
        await redis.delete(f"idem:reset:{key}")
        await redis.aclose()


async def test_reset_marker_ttl(redis_url):
    redis = Redis.from_url(redis_url)
    key = uuid.uuid4().hex
    idem = Idempotency(redis, "reset", key, "fp", in_flight_ttl_s=RESET_IN_FLIGHT_TTL_S)
    ttls: list[int] = []

    async def run() -> dict:
        ttls.append(await redis.ttl(f"idem:reset:{key}:lock"))
        return {}

    try:
        await idem.run(run)
        assert RESET_IN_FLIGHT_TTL_S == 300 and 60 < ttls[0] <= 300
    finally:
        await redis.delete(f"idem:reset:{key}")
        await redis.aclose()
