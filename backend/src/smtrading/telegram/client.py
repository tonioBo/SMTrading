"""Minimal async Telegram Bot API client (getMe, getChat, getUpdates, sendMessage,
sendDocument).

The token is part of every URL (`/bot<token>/<method>`), so nothing that can carry the URL
(httpx exceptions, responses, tracebacks) ever leaves this module: every failure becomes a
`TelegramError` with an Italian message, raised `from None` and outside any `except` (so
neither `__cause__` nor `__context__` holds the httpx error), with any echo of the token
replaced by `••••`.
"""

import json
import logging
import re
from typing import Any

import httpx

API_URL = "https://api.telegram.org"
_MASK = "••••"
_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]+")
DOCUMENT_TIMEOUT_S = 120.0  # an upload of up to 20 MB
log = logging.getLogger(__name__)


# What an error concerns (TelegramError.kind):
TRANSIENT = "transient"  # the whole chat, temporarily: no response, 5xx, 429
CHAT = "chat"  # the whole chat, until the configuration is fixed: token, bot, group, rights
MESSAGE = "message"  # this message only (any other error)

MSG_UNREACHABLE = "Telegram non raggiungibile"
MSG_BAD_TOKEN = "Token del bot non valido"
MSG_CHAT_NOT_FOUND = "Gruppo non trovato: controlla l'ID e che il bot sia nel gruppo"
MSG_NOT_MEMBER = "Il bot non è membro del gruppo o è stato rimosso"
MSG_NO_RIGHTS = "Il bot non ha i permessi per scrivere nel gruppo"
MSG_WAIT = "Telegram chiede di attendere"
MSG_GENERIC = "Errore Telegram (codice {code})"
_CHAT_MESSAGES = (MSG_BAD_TOKEN, MSG_CHAT_NOT_FOUND, MSG_NOT_MEMBER, MSG_NO_RIGHTS)


class TelegramError(Exception):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: int | None = None, migrate_to_chat_id: int | None = None,
                 kind: str = MESSAGE):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id
        self.kind = kind

    @property
    def chat_wide(self) -> bool:
        return self.kind != MESSAGE


def kind_of_message(message: str | None) -> str:
    """The kind of a stored error message (`last_error`) written from a TelegramError of
    this module: lets a restarted sender recognise a chat-wide error."""
    if not message:
        return MESSAGE
    if message.startswith(_CHAT_MESSAGES):
        return CHAT
    if message.startswith((MSG_UNREACHABLE, MSG_WAIT)):
        return TRANSIENT
    prefix = MSG_GENERIC.split("{", 1)[0]
    if message.startswith(prefix + "5"):
        return TRANSIENT
    return MESSAGE


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient, *, timeout: float = 10.0):
        """The token must look like `<bot id>:<secret>` (after strip): anything else (a
        newline, `#`, `/`, a control character) would change the request URL."""
        token = token.strip() if isinstance(token, str) else ""
        if not _TOKEN_RE.fullmatch(token):
            raise TelegramError(MSG_BAD_TOKEN, kind=CHAT) from None
        self._token = token
        self._http = http
        self._timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _MASK) if self._token else text

    async def _call(self, method: str, payload: dict[str, Any] | None = None, *,
                    files: dict[str, tuple[str, bytes, str]] | None = None,
                    timeout: float | None = None) -> Any:
        """JSON body, or with `files` a multipart form (`payload` as its fields)."""
        url = f"{API_URL}/bot{self._token}/{method}"
        resp = None
        body_kw: dict[str, Any] = ({"data": payload or {}, "files": files} if files
                                   else {"json": payload or {}})
        try:
            resp = await self._http.post(url, **body_kw, timeout=timeout or self._timeout)
        except (httpx.HTTPError, httpx.InvalidURL):
            pass  # raised below, outside the handler: no __context__ holding the URL
        if resp is None:
            raise TelegramError(MSG_UNREACHABLE, kind=TRANSIENT) from None
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
        low = description.lower()
        kind = MESSAGE
        if migrate is not None:
            msg = f"Il gruppo è diventato un supergruppo (nuovo ID {migrate})"
        elif code in (401, 404):  # 404: Telegram's answer to an unknown bot token
            msg, kind = MSG_BAD_TOKEN, CHAT
        elif code == 400 and "chat not found" in low:
            msg, kind = MSG_CHAT_NOT_FOUND, CHAT
        elif code == 400 and ("not enough rights" in low or "no rights" in low):
            msg, kind = MSG_NO_RIGHTS, CHAT
        elif code == 403:
            msg, kind = MSG_NOT_MEMBER, CHAT
        elif code == 409 and method == "getUpdates":
            msg = "Il bot ha un webhook attivo: rimuovilo con deleteWebhook"
        elif code == 429:
            msg = f"{MSG_WAIT} {retry_after if retry_after is not None else '?'} s"
            kind = TRANSIENT
        else:
            msg = f"{MSG_GENERIC.format(code=code)}: {description or 'nessuna descrizione'}"
            if code >= 500:
                kind = TRANSIENT
        return TelegramError(msg, status=code, retry_after=retry_after,
                             migrate_to_chat_id=migrate, kind=kind)

    async def _call_dict(self, method: str, payload: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
        r = await self._call(method, payload)
        return r if isinstance(r, dict) else {}

    async def get_me(self) -> dict[str, Any]:
        r = await self._call_dict("getMe")
        return {"id": r.get("id"), "username": r.get("username"),
                "first_name": r.get("first_name")}

    async def get_chat(self, chat_id: int | str) -> dict[str, Any]:
        r = await self._call_dict("getChat", {"chat_id": chat_id})
        return {"id": r.get("id"), "type": r.get("type"), "title": r.get("title")}

    async def get_updates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read only: no `offset`, so the pending updates are not confirmed (consumed)."""
        r = await self._call("getUpdates", {"limit": max(1, min(limit, 100))})
        return r if isinstance(r, list) else []

    async def send_message(self, chat_id: int | str, text: str, *,
                           entities: list[dict[str, Any]] | None = None,
                           disable_notification: bool = False) -> int | None:
        """Plain text: never `parse_mode`; formatting only via `entities`. Returns the
        message_id, or None when Telegram said ok without one: the message was delivered,
        so it must not be retried (a retry would duplicate it)."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if entities:
            payload["entities"] = entities
        payload["disable_notification"] = disable_notification
        payload["link_preview_options"] = {"is_disabled": True}
        r = await self._call_dict("sendMessage", payload)
        message_id = _int_or_none(r.get("message_id"))
        if message_id is None:
            log.warning("telegram sendMessage ok without a message_id: treated as sent")
        return message_id

    async def send_document(self, chat_id: int | str, filename: str, content: bytes, *,
                            caption: str | None = None,
                            caption_entities: list[dict[str, Any]] | None = None,
                            disable_notification: bool = False,
                            mime_type: str = "application/pdf") -> int | None:
        """A file as a document (multipart upload), with an optional plain caption
        (formatting only via `caption_entities`). Returns the message_id, None as for
        `send_message`."""
        payload: dict[str, Any] = {"chat_id": str(chat_id),
                                   "disable_notification": json.dumps(disable_notification)}
        if caption:
            payload["caption"] = caption
        if caption_entities:
            payload["caption_entities"] = json.dumps(caption_entities)
        r = await self._call("sendDocument", payload,
                             files={"document": (filename, content, mime_type)},
                             timeout=max(self._timeout, DOCUMENT_TIMEOUT_S))
        message_id = _int_or_none(r.get("message_id")) if isinstance(r, dict) else None
        if message_id is None:
            log.warning("telegram sendDocument ok without a message_id: treated as sent")
        return message_id
