"""Idempotency-Key support for the admin money endpoints.

With an `Idempotency-Key` header, the first successful execution stores its JSON response
in Redis at `idem:{scope}:{key}` as {status, body} (TTL 24h); a repetition with the same
key gets that response back, identical, without running again. While the first request
is still running (marker `idem:{scope}:{key}:lock`, SET NX, TTL 60s) a repetition gets
409. Failed executions are not stored: they changed nothing (the handler rolled back), so
the client may retry with the same key. Without the header the request just runs.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from smtrading.admin.auth import get_redis
from smtrading.common.errors import ErrorCode, SMTError

IDEMPOTENCY_HEADER = "Idempotency-Key"
RESPONSE_TTL_S = 24 * 3600
IN_FLIGHT_TTL_S = 60
MAX_KEY_LEN = 128


@dataclass
class Idempotency:
    redis: Redis
    scope: str
    key: str | None

    def _response_key(self) -> str:
        return f"idem:{self.scope}:{self.key}"

    @staticmethod
    def _replay(raw: bytes | str) -> JSONResponse:
        stored = json.loads(raw)
        return JSONResponse(stored["body"], status_code=stored["status"])

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> JSONResponse:
        """`fn` returns the JSON-ready body of a 200 response (and commits its work)."""
        if self.key is None:
            return JSONResponse(await fn())
        response_key = self._response_key()
        lock_key = response_key + ":lock"
        if (raw := await self.redis.get(response_key)) is not None:
            return self._replay(raw)
        if not await self.redis.set(lock_key, "1", nx=True, ex=IN_FLIGHT_TTL_S):
            return JSONResponse(
                {
                    "code": str(ErrorCode.INVALID_ORDER),
                    "message": "richiesta con la stessa Idempotency-Key già in corso",
                    "details": {"idempotency_key": self.key},
                },
                status_code=409,
            )
        try:
            # the first request may have finished between the read and the SET NX
            if (raw := await self.redis.get(response_key)) is not None:
                return self._replay(raw)
            body = await fn()
            await self.redis.set(
                response_key, json.dumps({"status": 200, "body": body}), ex=RESPONSE_TTL_S
            )
            return JSONResponse(body)
        finally:
            await self.redis.delete(lock_key)


def idempotent(scope: str) -> Callable[[Request], Idempotency]:
    """FastAPI dependency factory: `idem: Annotated[Idempotency, Depends(idempotent("x"))]`."""

    def dependency(request: Request) -> Idempotency:
        key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip() or None
        if key is not None and len(key) > MAX_KEY_LEN:
            raise SMTError(
                ErrorCode.INVALID_ORDER,
                f"Idempotency-Key troppo lunga (max {MAX_KEY_LEN} caratteri)",
                {"field": IDEMPOTENCY_HEADER},
            )
        return Idempotency(get_redis(request), scope, key)

    return dependency
