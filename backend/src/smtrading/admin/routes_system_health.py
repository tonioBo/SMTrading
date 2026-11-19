"""Admin API «Salute del sistema»: `GET /api/system-health` (admin and viewer, read-only),
the view of `admin.system_health`, computed at most once every CACHE_S seconds per api
process (concurrent requests share one computation)."""

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.system_health import system_health
from smtrading.mcp.dto import to_jsonable

router = APIRouter(prefix="/api", tags=["system-health"], dependencies=[Depends(require_access)])

CACHE_S = 30.0
_cache: dict[str, Any] = {"at": None, "value": None}
_lock = asyncio.Lock()


def clear_cache() -> None:
    _cache.update(at=None, value=None)


@router.get("/system-health")
async def get_system_health(session: DbSession, redis: RedisClient) -> dict:
    async with _lock:
        at = _cache["at"]
        if at is None or time.monotonic() - at >= CACHE_S:
            _cache["value"] = to_jsonable(await system_health(session, redis))
            _cache["at"] = time.monotonic()
        return _cache["value"]
