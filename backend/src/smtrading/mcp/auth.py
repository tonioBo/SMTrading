"""Bearer-token authentication for the /mcp endpoint (pure ASGI middleware).

Requests to `/mcp` and `/mcp/...` need `Authorization: Bearer smt_...`; anything else
passes through untouched. A missing or invalid token gets HTTP 401
`{"code":"FORBIDDEN","message":"invalid token"}` before the MCP protocol starts. On
success the agent id is set on `current_agent` for the duration of the request.
"""

import json
from collections.abc import Callable
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from smtrading.agents.service import authenticate, token_cache
from smtrading.common.errors import SMTError

current_agent: ContextVar[UUID | None] = ContextVar("smt_current_agent", default=None)

MCP_PATH = "/mcp"
_UNAUTHORIZED = json.dumps({"code": "FORBIDDEN", "message": "invalid token"}).encode()


def is_mcp_path(path: str) -> bool:
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


async def _send_401(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_UNAUTHORIZED)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _UNAUTHORIZED})


class McpAuthMiddleware:
    """`get_sessionmaker` is called per cache miss (the runtime is built in the lifespan)."""

    def __init__(
        self, app: ASGIApp, get_sessionmaker: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self.app = app
        self._get_sessionmaker = get_sessionmaker

    async def _resolve(self, token: str | None) -> UUID | None:
        if not token:
            return None
        agent_id = token_cache.get(token)
        if agent_id is not None:
            return agent_id
        async with self._get_sessionmaker()() as session:
            try:
                agent = await authenticate(session, token)
            except SMTError:
                return None
            agent_id = agent.id
            await session.commit()  # last_seen_at
        return agent_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_mcp_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        agent_id = await self._resolve(_bearer(scope))
        if agent_id is None:
            await _send_401(send)
            return
        reset = current_agent.set(agent_id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(reset)
