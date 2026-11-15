"""Writes the activity log, for every unsafe /api request, whatever the route.

`ActivityMiddleware` (pure ASGI, around the whole app) opens an ActivityContext for each
POST/PUT/PATCH/DELETE under /api: it keeps the request body (up to 64 KB, for the masked
summary), the answer's status and error body. The routing fills the route template and
the path parameters; `require_session_user` and the login handler note who acts.

Where the row is written:
- in the request's own transaction: `bind_session` (called by `get_db`) hooks the
  request session; its first commit adds the row inside a savepoint (`before_commit`). A
  state change that commits therefore always has its row; a failing savepoint is logged
  and never fails the commit.
- after the answer, in a short transaction of its own, when no commit carried it (a
  refused or failed request, a Redis-only action like the logout), plus a linked error row
  (same request_id) when the request failed after its commit, plus the extra access
  events (`note_event`).
A failure of the log never changes the answer: it is logged, and a «system» Telegram
alert goes out once, until a row is written again.
"""

import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.useractivity.context import ActivityContext, _current, current

log = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_INFO_KEY = "smt_user_activity"
ALERT_TEXT = ("Sicurezza: il Registro attività del portale non riesce a registrare le azioni "
              "degli utenti (vedi i log dell'API)")
_alerted = False  # a failure alert went out; cleared by the next row written

__all__ = ["ActivityContext", "ActivityMiddleware", "bind_session"]


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class ActivityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] in _SAFE_METHODS
                or not _is_api(scope["path"])):
            return await self.app(scope, receive, send)
        ctx = ActivityContext(scope)
        token = _current.set(ctx)

        async def receive_tee():
            message = await receive()
            if message["type"] == "http.request":
                ctx.add_body(message.get("body", b""))
            return message

        async def send_tee(message):
            if message["type"] == "http.response.start":
                ctx.status = message["status"]
            elif message["type"] == "http.response.body" and (ctx.status or 0) >= 400:
                ctx.add_error_body(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive_tee, send_tee)
        finally:
            _current.reset(token)
            await _finish(scope, ctx)


def _sessionmaker(scope):
    app = scope.get("app")
    return getattr(getattr(app, "state", None), "sessionmaker", None)


async def _finish(scope, ctx: ActivityContext) -> None:
    """After the answer: the rows no commit carried (see the module docstring)."""
    try:
        rows = []
        failed = (ctx.status or 500) >= 400
        if not ctx.committed or failed:
            rows.append(ctx.build_row())
        rows.extend(ctx.build_row(action=a) for a in ctx.events)
    except Exception:
        log.exception("activity log: row of %s %s not built", ctx.method, scope.get("path"))
        await _alert(_sessionmaker(scope))
        return
    if not rows:
        return
    sessionmaker = _sessionmaker(scope)
    if sessionmaker is None:  # no lifespan (some tests): nothing to write to
        return
    try:
        await _insert_rows(sessionmaker, rows)
    except Exception:
        log.exception("activity log: %d row(s) of %s %s not written", len(rows), ctx.method,
                      scope.get("path"))
        await _alert(sessionmaker)
        return
    _written()


async def _insert_rows(sessionmaker, rows) -> None:
    async with sessionmaker() as session:
        session.add_all(rows)
        await session.commit()


def _written() -> None:
    global _alerted
    _alerted = False


async def _alert(sessionmaker) -> None:
    """The «system» Telegram alert of a log failure, once until a row is written again."""
    global _alerted
    if _alerted or sessionmaker is None:
        return
    _alerted = True
    try:
        from smtrading.activity.models import ActivityLevel
        from smtrading.telegram.config import is_ready, load_config
        from smtrading.telegram.outbox import enqueue

        async with sessionmaker() as session:
            cfg = await load_config(session)
            if is_ready(cfg):
                await enqueue(session, source="system", category="event",
                              level=str(ActivityLevel.critical), text=ALERT_TEXT, cfg=cfg)
                await session.commit()
    except Exception:
        log.exception("activity log: the failure alert could not be queued")


# --- the request session's commit ------------------------------------------------------


def bind_session(session: AsyncSession) -> None:
    """Hook the request's session: its first commit carries the activity row."""
    ctx = current()
    if ctx is None:
        return
    sync = session.sync_session
    sync.info[_INFO_KEY] = ctx
    event.listen(sync, "before_commit", _before_commit)
    event.listen(sync, "after_commit", _after_commit)
    event.listen(sync, "after_rollback", _after_rollback)


def _before_commit(sync_session) -> None:
    ctx: ActivityContext | None = sync_session.info.get(_INFO_KEY)
    # a savepoint's commit fires the event too: only the outermost commit carries the row
    if ctx is None or ctx.committed or ctx.pending or sync_session.in_nested_transaction():
        return
    try:
        row = ctx.build_row(provisional=True)
        with sync_session.begin_nested():
            sync_session.add(row)
        ctx.pending = True
    except Exception:  # never fails the commit: the row is written after the answer
        log.exception("activity log: row not added to the request's commit")


def _after_commit(sync_session) -> None:
    ctx: ActivityContext | None = sync_session.info.get(_INFO_KEY)
    if ctx is not None and ctx.pending:
        ctx.pending, ctx.committed = False, True
        _written()


def _after_rollback(sync_session) -> None:
    ctx: ActivityContext | None = sync_session.info.get(_INFO_KEY)
    if ctx is not None:
        ctx.pending = False
