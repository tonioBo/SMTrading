"""Paperclip issue writer: the one write SMTrading makes to Paperclip for the protection
alarm (`positions.protection`). `IssuesOnlyTransport` lets through GET requests and a
POST to the company's `issues` only, refused before any network I/O otherwise; the
reader's client stays GET-only. Errors are short Italian texts that never contain the key.

Endpoints (verified on the real server): `GET /api/companies/{id}/agents` (id, name) and
`POST /api/companies/{id}/issues` with {title, description, status, priority,
assigneeAgentId} -> {identifier, ...}; `GET /api/companies/{id}/issues` (a list of {id,
identifier, title, status}) is read after a timed-out POST, which may have created the
issue anyway."""

from typing import Any, Self
from urllib.parse import quote

import httpx

from smtrading.common.logsetup import register_secret
from smtrading.paperclip.client import PaperclipError, unwrap_list
from smtrading.paperclip.config import PaperclipConfig

WRITE_REFUSED = "Paperclip: scrittura non consentita"
RISK_MANAGER = "Risk Manager"
CLOSED_STATUSES = frozenset({"done", "cancelled", "canceled"})


class IssuesOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, issues_path: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._issues_path = issues_path
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        allowed = request.method == "GET" or (
            request.method == "POST"
            and request.url.raw_path.split(b"?", 1)[0].decode("ascii") == self._issues_path)
        if not allowed:
            raise RuntimeError(WRITE_REFUSED)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def find_agent_id(agents: list[dict], name: str = RISK_MANAGER) -> str | None:
    """The id of the agent called `name` (case and spaces ignored)."""
    want = " ".join(name.split()).casefold()
    for a in agents:
        if a.get("id") and " ".join(str(a.get("name") or "").split()).casefold() == want:
            return str(a["id"])
    return None


async def resolve_agent(client: "PaperclipIssues", name: str) -> str:
    """The id of the Paperclip agent called `name`, else PaperclipError."""
    agent_id = find_agent_id(await client.agents(), name)
    if agent_id is None:
        raise PaperclipError(f"agente «{name}» non trovato")
    return agent_id


class PaperclipIssues:
    def __init__(self, cfg: PaperclipConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0) -> None:
        if not cfg.complete:
            raise PaperclipError("configurazione incompleta: URL, id azienda e chiave board")
        register_secret(cfg.board_key or "")
        base = str(cfg.base_url).rstrip("/")
        self._company_path = f"/api/companies/{quote(str(cfg.company_id), safe='')}"
        prefix = httpx.URL(base).raw_path.decode("ascii").rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=base,
            transport=IssuesOnlyTransport(f"{prefix}{self._company_path}/issues", transport),
            timeout=timeout,
            headers={"Authorization": f"Bearer {cfg.board_key}", "Accept": "application/json"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            r = await self._http.request(method, path, json=body)
        except httpx.TimeoutException:
            raise PaperclipError("timeout") from None
        except httpx.HTTPError:
            raise PaperclipError("non raggiungibile") from None
        if r.status_code >= 300:
            raise PaperclipError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise PaperclipError("risposta non JSON") from None

    async def agents(self) -> list[dict]:
        body = await self._call("GET", f"{self._company_path}/agents")
        return [a for a in unwrap_list(body, "agents") if isinstance(a, dict)]

    async def create_issue(self, *, title: str, description: str, assignee_agent_id: str,
                           priority: str = "critical") -> str:
        """The new issue's identifier (its id when the identifier is missing)."""
        body = await self._call("POST", f"{self._company_path}/issues", {
            "title": title, "description": description, "status": "todo",
            "priority": priority, "assigneeAgentId": assignee_agent_id,
        })
        ident = (body.get("identifier") or body.get("id")) if isinstance(body, dict) else None
        if not ident:
            raise PaperclipError("risposta inattesa")
        return str(ident)

    async def open_issue_with_title(self, title: str) -> str | None:
        """The identifier (else id) of an open issue (not done or cancelled) titled
        exactly `title`, or None."""
        body = await self._call("GET", f"{self._company_path}/issues")
        for issue in unwrap_list(body, "issues"):
            if (isinstance(issue, dict) and issue.get("title") == title
                    and str(issue.get("status") or "").lower() not in CLOSED_STATUSES):
                ident = issue.get("identifier") or issue.get("id")
                if ident:
                    return str(ident)
        return None

    async def open_identifiers(self) -> set[str]:
        """The identifiers (else ids) of the issues not done or cancelled, from one GET."""
        body = await self._call("GET", f"{self._company_path}/issues")
        found: set[str] = set()
        for issue in unwrap_list(body, "issues"):
            if (isinstance(issue, dict)
                    and str(issue.get("status") or "").lower() not in CLOSED_STATUSES):
                ident = issue.get("identifier") or issue.get("id")
                if ident:
                    found.add(str(ident))
        return found
