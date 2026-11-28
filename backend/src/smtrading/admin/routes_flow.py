"""Admin API of the «Fiume dei flussi» page: read only, admin only, never MCP.

    GET /api/flow/map                       nodes and links (flow.map, the single source)
    GET /api/flow/events?after=&limit=      {events, cursor, now}: the events with
                                            ts > after − 10 s (no `after`: the last 15
                                            minutes), at most `limit` (1..2000, default
                                            500), oldest first; `cursor` = the latest ts
                                            returned (else `after`, else `now`)
    GET /api/flow/summary?window=1|5|15     per link, per node and head counters

A bad parameter answers 422 (code INVALID_ORDER, details.fields), never echoing it.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from smtrading.admin.auth import DbSession, require_admin
from smtrading.common.errors import ErrorCode
from smtrading.flow.events import LIMIT_DEFAULT, LIMIT_MAX, LIMIT_MIN, flow_events
from smtrading.flow.map import flow_map
from smtrading.flow.summary import WINDOWS, flow_summary

router = APIRouter(prefix="/api", tags=["flow"], dependencies=[Depends(require_admin)])

DEFAULT_WINDOW = 5


class _BadParam(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field, self.message = field, message


def _unprocessable(e: _BadParam) -> JSONResponse:
    return JSONResponse({"code": str(ErrorCode.INVALID_ORDER), "message": e.message,
                         "details": {"fields": [e.field]}}, status_code=422)


def _int(raw: str | None, field: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise _BadParam(field, f"{field}: serve un numero intero") from None


def _time(raw: str | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:  # a «+» sent unencoded arrives as a space
        t = datetime.fromisoformat(raw.strip().replace(" ", "+"))
    except ValueError:
        raise _BadParam("after", "after: serve un'ora ISO 8601") from None
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


@router.get("/flow/map")
async def get_flow_map(session: DbSession) -> dict:
    return await flow_map(session)


@router.get("/flow/events")
async def get_flow_events(session: DbSession, after: str | None = None,
                          limit: str | None = None):
    try:
        cursor = _time(after)
        n = _int(limit, "limit", LIMIT_DEFAULT)
        if not LIMIT_MIN <= n <= LIMIT_MAX:
            raise _BadParam("limit", f"limit: tra {LIMIT_MIN} e {LIMIT_MAX}")
    except _BadParam as e:
        return _unprocessable(e)
    now = datetime.now(UTC)
    events = await flow_events(session, after=cursor, limit=n, now=now)
    latest = max((e["ts"] for e in events), default=None)
    if cursor is not None and (latest is None or latest < cursor):
        latest = cursor  # a late MCP row (older ts) never moves the cursor back
    return {"events": events, "cursor": latest or now, "now": now}


@router.get("/flow/summary")
async def get_flow_summary(session: DbSession, window: str | None = None):
    try:
        w = _int(window, "window", DEFAULT_WINDOW)
        if w not in WINDOWS:
            raise _BadParam("window", "window: 1, 5 o 15 minuti")
    except _BadParam as e:
        return _unprocessable(e)
    return await flow_summary(session, window_min=w, now=datetime.now(UTC))
