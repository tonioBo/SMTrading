"""Idempotency-Key support for the admin money endpoints.

With an `Idempotency-Key` header, the first successful execution stores its JSON response
in Redis at `idem:{scope}:{key}` as {status, body, fingerprint} (TTL 24h); a repetition
with the same key gets that response back, identical, without running again. The
fingerprint is method + path + sha256(body): the same key with a different request gets
422 instead. While the first request is still running (marker `idem:{scope}:{key}:lock`
holding a random token, SET NX, TTL 60s — 300s for a reset — and refreshed by its owner
while the request runs) a repetition gets 409; the marker is released only by its owner
(compare-and-delete), never after it expired and was taken again. Failed executions are not stored: they changed nothing (the handler rolled back), so
the client may retry with the same key. Without the header the request just runs.
"""

import asyncio
import contextlib
import hashlib
import json
import secrets
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
RESET_IN_FLIGHT_TTL_S = 300  # a reset closes positions: it can take a while
MAX_KEY_LEN = 128
# delete KEYS[1] only if it still holds our token
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)
# extend KEYS[1] to ARGV[2] seconds only if it still holds our token
_REFRESH_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)


def _error(status: int, message: str, key: str) -> JSONResponse:
    return JSONResponse(
        {
            "code": str(ErrorCode.INVALID_ORDER), "message": message,
            "details": {"idempotency_key": key},
        },
        status_code=status,
    )


@dataclass
class Idempotency:
    redis: Redis
    scope: str
    key: str | None
    fingerprint: str = ""
    in_flight_ttl_s: int = IN_FLIGHT_TTL_S

    def _response_key(self) -> str:
        return f"idem:{self.scope}:{self.key}"

    def _replay(self, raw: bytes | str) -> JSONResponse:
        stored = json.loads(raw)
        if stored.get("fingerprint") != self.fingerprint:
            return _error(422, "Idempotency-Key riutilizzata con dati diversi", self.key or "")
        return JSONResponse(stored["body"], status_code=stored["status"])

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> JSONResponse:
        """`fn` returns the JSON-ready body of a 200 response (and commits its work)."""
        if self.key is None:
            return JSONResponse(await fn())
        response_key = self._response_key()
        lock_key = response_key + ":lock"
        if (raw := await self.redis.get(response_key)) is not None:
            return self._replay(raw)
        token = secrets.token_hex(16)
        if not await self.redis.set(lock_key, token, nx=True, ex=self.in_flight_ttl_s):
            return _error(409, "richiesta con la stessa Idempotency-Key già in corso", self.key)
        refresher = asyncio.create_task(self._keep_lock(lock_key, token))
        try:
            # the first request may have finished between the read and the SET NX
            if (raw := await self.redis.get(response_key)) is not None:
                return self._replay(raw)
            body = await fn()
            await self.redis.set(
                response_key,
                json.dumps({"status": 200, "body": body, "fingerprint": self.fingerprint}),
                ex=RESPONSE_TTL_S,
            )
            return JSONResponse(body)
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            await self.redis.eval(_RELEASE_LUA, 1, lock_key, token)

    async def _keep_lock(self, lock_key: str, token: str) -> None:
        """Re-arm the in-flight marker every third of its TTL while the request runs."""
        while True:
            await asyncio.sleep(self.in_flight_ttl_s / 3)
            try:
                await self.redis.eval(_REFRESH_LUA, 1, lock_key, token, self.in_flight_ttl_s)
            except Exception:  # noqa: BLE001 - best effort: the TTL still bounds the marker
                return


def idempotent(
    scope: str, *, in_flight_ttl_s: int = IN_FLIGHT_TTL_S
) -> Callable[[Request], Awaitable[Idempotency]]:
    """FastAPI dependency factory: `idem: Annotated[Idempotency, Depends(idempotent("x"))]`."""

    async def dependency(request: Request) -> Idempotency:
        key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip() or None
        if key is not None and len(key) > MAX_KEY_LEN:
            raise SMTError(
                ErrorCode.INVALID_ORDER,
                f"Idempotency-Key troppo lunga (max {MAX_KEY_LEN} caratteri)",
                {"field": IDEMPOTENCY_HEADER},
            )
        fingerprint = ""
        if key is not None:
            digest = hashlib.sha256(await request.body()).hexdigest()
            fingerprint = f"{request.method} {request.url.path} {digest}"
        return Idempotency(get_redis(request), scope, key, fingerprint, in_flight_ttl_s)

    return dependency
