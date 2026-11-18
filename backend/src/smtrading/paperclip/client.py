"""Paperclip client, read-only but for one write. Board keys cannot be scoped read-only in
Paperclip, so the rule is enforced here: `GetOnlyTransport` refuses every method but GET
before any network I/O; a client built with `allow_session_reset=True` also lets through
`POST /api/agents/{id}/runtime-state/reset-session` (the agents' session refresh after an
MCP tool catalog change), under the base URL's path prefix when there is one, and nothing
else. Errors are short Italian texts that never
contain the key."""

import re
from typing import Any, Self
from urllib.parse import quote

import httpx

from smtrading.common.logsetup import register_secret
from smtrading.paperclip.config import PaperclipConfig

READ_ONLY_MESSAGE = "Paperclip: solo lettura"
MAX_RUNS = 1000
_ENVELOPES = ("items", "data", "results")


def session_reset_path(prefix: str = "") -> re.Pattern[str]:
    """The reset path under `prefix` (the base URL's raw path, e.g. `/pc`)."""
    return re.compile(rf"^{re.escape(prefix.rstrip('/'))}/api/agents/[^/]+/runtime-state"
                      r"/reset-session$")


SESSION_RESET_PATH = session_reset_path()


class PaperclipError(Exception):
    """A failed read; `message` is safe to show and store (no key, no URL)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GetOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None, *,
                 allow_session_reset: bool = False, prefix: str = "") -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._allow_session_reset = allow_session_reset
        self._reset_path = session_reset_path(prefix)

    def _allowed(self, request: httpx.Request) -> bool:
        if request.method == "GET":
            return True
        # the raw (still encoded) path: an id is one segment, "%2F" included
        path = request.url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
        return (self._allow_session_reset and request.method == "POST"
                and self._reset_path.match(path) is not None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._allowed(request):
            raise RuntimeError(READ_ONLY_MESSAGE)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def unwrap_list(body: Any, *names: str) -> list:
    """A root list, or the list under one of `names` / the usual envelopes."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for name in (*names, *_ENVELOPES):
            if isinstance(body.get(name), list):
                return body[name]
    raise PaperclipError("risposta inattesa")


class PaperclipClient:
    def __init__(self, cfg: PaperclipConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0, allow_session_reset: bool = False) -> None:
        if not cfg.complete:
            raise PaperclipError("configurazione incompleta: URL, id azienda e chiave board")
        register_secret(cfg.board_key or "")
        self._company = quote(str(cfg.company_id), safe="")
        base = str(cfg.base_url).rstrip("/")
        prefix = httpx.URL(base).raw_path.decode("ascii").rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=base,
            transport=GetOnlyTransport(transport, allow_session_reset=allow_session_reset,
                                       prefix=prefix),
            timeout=timeout,
            headers={"Authorization": f"Bearer {cfg.board_key}", "Accept": "application/json"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params={
            k: v for k, v in (params or {}).items() if v is not None})

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise PaperclipError("timeout") from None
        except httpx.HTTPError:
            raise PaperclipError("non raggiungibile") from None
        if r.status_code >= 300:  # redirects are not followed: an error too
            raise PaperclipError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PaperclipError("risposta non JSON") from None

    def _company_path(self, tail: str) -> str:
        return f"/api/companies/{self._company}/{tail}"

    async def company(self) -> dict:
        """The company (`GET /api/companies/{id}`): `budgetMonthlyCents`,
        `spentMonthlyCents` …"""
        body = await self._get(f"/api/companies/{self._company}")
        if isinstance(body, dict) and isinstance(body.get("company"), dict):
            body = body["company"]
        if not isinstance(body, dict):
            raise PaperclipError("risposta inattesa")
        return body

    async def heartbeat_runs(self, limit: int = MAX_RUNS) -> list[dict]:
        body = await self._get(self._company_path("heartbeat-runs"), {"limit": limit})
        return [r for r in unwrap_list(body, "runs") if isinstance(r, dict)]

    async def agents(self) -> list[dict]:
        body = await self._get(self._company_path("agents"))
        return [a for a in unwrap_list(body, "agents") if isinstance(a, dict)]

    async def tool_policies(self) -> list[dict]:
        body = await self._get(self._company_path("tools/policies"))
        return [p for p in unwrap_list(body, "policies") if isinstance(p, dict)]

    async def current_key_id(self) -> str | None:
        """The id of the key in use: `keyId` of `GET /api/cli-auth/me`."""
        body = await self._get("/api/cli-auth/me")
        key_id = body.get("keyId") if isinstance(body, dict) else None
        return str(key_id) if key_id else None

    async def reset_session(self, agent_id: str) -> Any:
        """Clears the agent's Paperclip session (`sessionId` null): its next run starts a
        new one, with the current MCP tool catalog. Needs `allow_session_reset=True`."""
        path = f"/api/agents/{quote(str(agent_id), safe='')}/runtime-state/reset-session"
        return await self._request("POST", path, json={})

    async def board_api_keys(self) -> list[dict]:
        body = await self._get("/api/board-api-keys")
        return [k for k in unwrap_list(body) if isinstance(k, dict)]
