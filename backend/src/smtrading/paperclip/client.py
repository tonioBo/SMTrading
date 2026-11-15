"""Read-only Paperclip client. Board keys cannot be scoped read-only in Paperclip, so the
read-only rule is enforced here: `GetOnlyTransport` refuses every method but GET before
any network I/O. Errors are short Italian texts that never contain the key."""

from typing import Any, Self
from urllib.parse import quote

import httpx

from smtrading.common.logsetup import register_secret
from smtrading.paperclip.config import PaperclipConfig

READ_ONLY_MESSAGE = "Paperclip: solo lettura"
MAX_RUNS = 1000
_ENVELOPES = ("items", "data", "results")


class PaperclipError(Exception):
    """A failed read; `message` is safe to show and store (no key, no URL)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GetOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
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
                 timeout: float = 10.0) -> None:
        if not cfg.complete:
            raise PaperclipError("configurazione incompleta: URL, id azienda e chiave board")
        register_secret(cfg.board_key or "")
        self._company = quote(str(cfg.company_id), safe="")
        self._http = httpx.AsyncClient(
            base_url=str(cfg.base_url).rstrip("/"),
            transport=GetOnlyTransport(transport),
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
        try:
            r = await self._http.get(path, params={k: v for k, v in (params or {}).items()
                                                   if v is not None})
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

    async def costs_by_agent(self, from_: str | None = None, to: str | None = None) -> Any:
        return await self._get(self._company_path("costs/by-agent"), {"from": from_, "to": to})

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
        """The id of the key in use (`GET /api/cli-auth/me`), when Paperclip tells it."""
        body = await self._get("/api/cli-auth/me")
        if not isinstance(body, dict):
            return None
        for name in ("keyId", "apiKeyId", "boardApiKeyId"):
            if body.get(name):
                return str(body[name])
        key = body.get("key") or body.get("apiKey")
        return str(key["id"]) if isinstance(key, dict) and key.get("id") else None

    async def board_api_keys(self) -> list[dict]:
        body = await self._get("/api/board-api-keys")
        return [k for k in unwrap_list(body, "keys") if isinstance(k, dict)]
