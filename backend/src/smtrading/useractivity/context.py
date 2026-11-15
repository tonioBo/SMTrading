"""The activity context of the current unsafe /api request (a ContextVar set by the
recorder's middleware) and the notes the handlers add to it: who acts, an action override,
extra access events. Every `note_*` is a no-op outside such a request (CLI, worker, MCP).
Light on imports: the admin and agents services use it."""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

BODY_MAX = 64 * 1024  # a longer body is summarized as "troppo grande"
ERROR_BODY_MAX = 8 * 1024


def outcome_of(status: int) -> str:
    if status < 400:
        return "ok"
    return "denied" if status in (401, 403) else "error"


@dataclass(eq=False)
class ActivityContext:
    scope: dict[str, Any]
    request_id: uuid.UUID = field(default_factory=uuid.uuid4)
    body: bytearray = field(default_factory=bytearray)
    body_truncated: bool = False
    error_body: bytearray = field(default_factory=bytearray)
    status: int | None = None
    user_id: uuid.UUID | None = None
    email: str | None = None
    role: str | None = None
    override: str | None = None
    events: list[str] = field(default_factory=list)
    pending: bool = False  # a row was added to the request session's committing transaction
    committed: bool = False  # ... and that transaction committed
    _summary: dict[str, Any] | None = None

    @property
    def method(self) -> str:
        return self.scope["method"]

    def add_body(self, chunk: bytes) -> None:
        if self.body_truncated or not chunk:
            return
        if len(self.body) + len(chunk) > BODY_MAX:
            self.body_truncated = True
            self.body.clear()
        else:
            self.body.extend(chunk)

    def add_error_body(self, chunk: bytes) -> None:
        room = ERROR_BODY_MAX - len(self.error_body)
        if room > 0 and chunk:
            self.error_body.extend(chunk[:room])

    def _header(self, name: bytes) -> str | None:
        for k, v in self.scope.get("headers") or ():
            if k.lower() == name:
                return v.decode("latin-1")
        return None

    def summary(self) -> dict[str, Any]:
        from smtrading.useractivity.sanitize import summarize

        if self._summary is None:
            self._summary = summarize(bytes(self.body), self._header(b"content-type"),
                                      truncated=self.body_truncated)
        return self._summary

    def _error(self) -> tuple[str | None, str | None]:
        import json

        from smtrading.common.logsetup import redact_log_text

        try:
            data = json.loads(bytes(self.error_body))
        except (ValueError, UnicodeDecodeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        code, message = data.get("code"), data.get("message")
        return (str(code)[:40] if code else None,
                redact_log_text(str(message))[:300] if message else None)

    def build_row(self, *, provisional: bool = False, action: str | None = None):
        """The UserActivity row of this request. `provisional`: written by the request's
        own commit, before the answer exists (the route's success status)."""
        from smtrading.common.logsetup import redact_log_text
        from smtrading.useractivity.catalog import action_for
        from smtrading.useractivity.models import UserActivity
        from smtrading.useractivity.sanitize import target_of

        route = self.scope.get("route")
        template = getattr(route, "path", None)
        if provisional:
            status, code, message = getattr(route, "status_code", None) or 200, None, None
        else:
            status = self.status or 500
            code, message = self._error() if status >= 400 else (None, None)
        outcome = outcome_of(status)
        client = self.scope.get("client")
        user_agent = self._header(b"user-agent")
        return UserActivity(
            request_id=self.request_id, user_id=self.user_id, email=self.email,
            role=self.role,
            action=action or action_for(self.method, template, outcome, code, self.override),
            method=self.method,
            route=template or redact_log_text(self.scope.get("path", ""))[:200],
            target=target_of(self.scope.get("path_params")) if template else {},
            outcome=outcome, status=status, error_code=code, error_message=message,
            ip=client[0][:64] if client else None,
            user_agent=redact_log_text(user_agent)[:300] if user_agent else None,
            summary=self.summary(),
        )


_current: ContextVar[ActivityContext | None] = ContextVar("smt_user_activity", default=None)


def current() -> ActivityContext | None:
    return _current.get()


def current_request_id() -> str | None:
    ctx = _current.get()
    return str(ctx.request_id) if ctx is not None else None


def note_actor(user) -> None:
    """The person acting (an AdminUser)."""
    ctx = _current.get()
    if ctx is not None:
        ctx.user_id, ctx.email, ctx.role = user.id, user.email, user.role


def note_email(email: str) -> None:
    """Who is trying to log in (the attempted email, the user being still unknown)."""
    ctx = _current.get()
    if ctx is not None:
        ctx.email = email[:254]


def set_action(action: str) -> None:
    """A more precise action than the route's, kept when the request succeeds."""
    ctx = _current.get()
    if ctx is not None:
        ctx.override = action


def note_event(action: str) -> None:
    """An extra access event of this request (e.g. `auth.totp_lockout`), recorded as its
    own row with the request's outcome."""
    ctx = _current.get()
    if ctx is not None:
        ctx.events.append(action)
