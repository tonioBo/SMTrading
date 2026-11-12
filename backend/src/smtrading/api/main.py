import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine

log = logging.getLogger(__name__)
HEARTBEAT_KEY = "worker:heartbeat"
HEARTBEAT_MAX_AGE_S = 30.0


class _Resources:
    """Lazily created engine/redis client; nothing connects at import or app creation."""

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._redis: Redis | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = make_engine(get_settings().database_url)
        return self._engine

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(get_settings().redis_url)
        return self._redis

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def create_app() -> FastAPI:
    resources = _Resources()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await resources.close()

    app = FastAPI(title="SMTrading", lifespan=lifespan)
    app.state.close_resources = resources.close

    @app.get("/health")
    async def health(response: Response) -> dict:
        db_ok = False
        redis_ok = False
        age: float | None = None
        try:
            async with resources.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.warning("health check failed", exc_info=True)
        try:
            await resources.redis.ping()
            redis_ok = True
            raw = await resources.redis.get(HEARTBEAT_KEY)
            if raw is not None:
                age = max(0.0, time.time() - float(raw))
        except Exception:
            log.warning("health check failed", exc_info=True)
        healthy = db_ok and redis_ok and age is not None and age < HEARTBEAT_MAX_AGE_S
        if not healthy:
            response.status_code = 503
        return {
            "status": "ok" if healthy else "degraded",
            "db": db_ok,
            "redis": redis_ok,
            "worker_heartbeat_age_s": age,
        }

    return app
