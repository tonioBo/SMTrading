"""The worker heartbeat: the worker writes `time.time()` to `worker:heartbeat` every few
seconds; the API reads its age (health check, admin dashboard)."""

import time

HEARTBEAT_KEY = "worker:heartbeat"


async def write_heartbeat(redis) -> None:
    await redis.set(HEARTBEAT_KEY, str(time.time()))


async def heartbeat_age_s(redis) -> float | None:
    """Seconds since the last heartbeat; None when there is none. Redis errors propagate."""
    raw = await redis.get(HEARTBEAT_KEY)
    if raw is None:
        return None
    return max(0.0, time.time() - float(raw))
