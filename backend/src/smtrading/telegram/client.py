"""Minimal async Telegram Bot API client (getMe, getChat, getUpdates, sendMessage).

The token is part of every URL (`/bot<token>/<method>`), so nothing that can carry the URL
(httpx exceptions, responses, tracebacks) ever leaves this module: every failure becomes a
`TelegramError` with an Italian message, raised `from None` and outside any `except` (so
neither `__cause__` nor `__context__` holds the httpx error), with any echo of the token
replaced by `••••`.
"""

from typing import Any

import httpx

API_URL = "https://api.telegram.org"
_MASK = "••••"


class TelegramError(Exception):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: int | None = None, migrate_to_chat_id: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient, *, timeout: float = 10.0):
        self._token = token
        self._http = http
        self._timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _MASK) if self._token else text

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{API_URL}/bot{self._token}/{method}"
        resp = None
        try:
            resp = await self._http.post(url, json=payload or {}, timeout=self._timeout)
        except httpx.HTTPError:
            pass  # raised below, outside the handler: no __context__ holding the URL
        if resp is None:
            raise TelegramError("Telegram non raggiungibile") from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        if resp.status_code == 200 and body.get("ok") is True:
            return body.get("result")
        raise self._error(method, resp.status_code, body) from None

    def _error(self, method: str, status: int, body: dict[str, Any]) -> TelegramError:
        code = _int_or_none(body.get("error_code")) or status
        description = self._redact(str(body.get("description") or ""))[:300]
        params = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
        retry_after = _int_or_none(params.get("retry_after"))
        migrate = _int_or_none(params.get("migrate_to_chat_id"))
        if migrate is not None:
            msg = f"Il gruppo è diventato un supergruppo (nuovo ID {migrate})"
        elif code == 401:
            msg = "Token del bot non valido"
        elif code == 400 and "chat not found" in description.lower():
            msg = "Gruppo non trovato: controlla l'ID e che il bot sia nel gruppo"
        elif code == 403:
            msg = "Il bot non è membro del gruppo o è stato rimosso"
        elif code == 409 and method == "getUpdates":
            msg = "Il bot ha un webhook attivo: rimuovilo con deleteWebhook"
        elif code == 429:
            msg = f"Telegram chiede di attendere {retry_after if retry_after is not None else '?'} s"
        else:
            msg = f"Errore Telegram (codice {code}): {description or 'nessuna descrizione'}"
        return TelegramError(msg, status=code, retry_after=retry_after,
                             migrate_to_chat_id=migrate)

    async def get_me(self) -> dict[str, Any]:
        r = await self._call("getMe") or {}
        return {"id": r.get("id"), "username": r.get("username"),
                "first_name": r.get("first_name")}

    async def get_chat(self, chat_id: int | str) -> dict[str, Any]:
        r = await self._call("getChat", {"chat_id": chat_id}) or {}
        return {"id": r.get("id"), "type": r.get("type"), "title": r.get("title")}

    async def get_updates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read only: no `offset`, so the pending updates are not confirmed (consumed)."""
        r = await self._call("getUpdates", {"limit": max(1, min(limit, 100))})
        return r if isinstance(r, list) else []

    async def send_message(self, chat_id: int | str, text: str, *,
                           entities: list[dict[str, Any]] | None = None,
                           disable_notification: bool = False) -> int:
        """Plain text: never `parse_mode`; formatting only via `entities`."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if entities:
            payload["entities"] = entities
        payload["disable_notification"] = disable_notification
        payload["link_preview_options"] = {"is_disabled": True}
        r = await self._call("sendMessage", payload) or {}
        message_id = _int_or_none(r.get("message_id"))
        if message_id is None:
            raise TelegramError("Risposta di Telegram senza message_id")
        return message_id
