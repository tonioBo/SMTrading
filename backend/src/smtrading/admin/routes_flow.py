"""Admin API of the «Fiume dei flussi» page: read only, admin only, never MCP.

    GET /api/flow/map                       nodes, links, lens families and price sources
                                            (flow.map, the single source)
    GET /api/flow/events?after=&limit=      live: {events, cursor, now}: the events with
                                            ts > after − 10 s (no `after`: the last 15
                                            minutes), at most `limit` (1..2000, default
                                            500), oldest first; `cursor` = the latest ts
                                            returned (else `after`, else `now`)
    GET /api/flow/events?from=&to=&cursor=&limit=
                                            replay: {events, cursor, from, to, now}: one
                                            page of the closed window from ≤ ts ≤ to (at
                                            most 24 h; `to` after now is now), oldest
                                            first; `cursor` = the next page's (opaque) or
                                            null when the window is done
    GET /api/flow/summary?window=1|5|15|today   per link, per node and head counters
    GET /api/flow/tools?window=1|5|15|today     the lens: tools per family and per link
    GET /api/flow/order/{order_id}          the story of an order (404: unknown or hidden)
    GET /api/flow/mcp/{audit_id}            one agent's MCP call (404: unknown, admin row)
    GET /api/flow/agents                    Paperclip runs and costs (cached 30 s;
                                            Paperclip down: 200, «costi non disponibili»)
    GET /api/flow/health                    every node's health (cached 15 s)
    GET /api/flow/sources                   the price sources behind «Prezzi»

A bad parameter answers 422 (code INVALID_ORDER, details.fields), never echoing it.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.common.errors import ErrorCode
from smtrading.flow.agents import flow_agents
from smtrading.flow.events import (
    LIMIT_DEFAULT,
    LIMIT_MAX,
    LIMIT_MIN,
    REPLAY_MAX,
    decode_cursor,
    encode_cursor,
    flow_events,
    replay_events,
)
from smtrading.flow.health import flow_health
from smtrading.flow.map import flow_map
from smtrading.flow.sources import price_sources
from smtrading.flow.story import mcp_story, order_story
from smtrading.flow.summary import TODAY, WINDOWS, flow_summary, flow_tools
from smtrading.mcp.dto import to_jsonable

router = APIRouter(prefix="/api", tags=["flow"], dependencies=[Depends(require_admin)])

DEFAULT_WINDOW = 5
AUDIT_ID_MAX = 2**63 - 1


class _BadParam(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field, self.message = field, message


def _unprocessable(e: _BadParam) -> JSONResponse:
    return JSONResponse({"code": str(ErrorCode.INVALID_ORDER), "message": e.message,
                         "details": {"fields": [e.field]}}, status_code=422)


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"code": str(ErrorCode.NOT_FOUND), "message": message,
                         "details": {}}, status_code=404)


def _int(raw: str | None, field: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise _BadParam(field, f"{field}: serve un numero intero") from None


def _time(raw: str | None, field: str = "after") -> datetime | None:
    if raw is None or raw == "":
        return None
    try:  # a «+» sent unencoded arrives as a space
        t = datetime.fromisoformat(raw.strip().replace(" ", "+"))
    except ValueError:
        raise _BadParam(field, f"{field}: serve un'ora ISO 8601") from None
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _window(raw: str | None) -> int | str:
    if raw == TODAY:
        return TODAY
    w = _int(raw, "window", DEFAULT_WINDOW)
    if w not in WINDOWS:
        raise _BadParam("window", "window: 1, 5 o 15 minuti, oppure today")
    return w


def _limit(raw: str | None) -> int:
    n = _int(raw, "limit", LIMIT_DEFAULT)
    if not LIMIT_MIN <= n <= LIMIT_MAX:
        raise _BadParam("limit", f"limit: tra {LIMIT_MIN} e {LIMIT_MAX}")
    return n


@router.get("/flow/map")
async def get_flow_map(session: DbSession) -> dict:
    return await flow_map(session)


async def _replay(session, frm: str | None, to: str | None, cursor_raw: str | None,
                  n: int, now: datetime):
    since, until = _time(frm, "from"), _time(to, "to")
    if since is None or until is None:
        raise _BadParam("from" if since is None else "to", "from e to vanno dati insieme")
    until = min(until, now)
    if since >= until:
        raise _BadParam("from", "from deve venire prima di to")
    if until - since > REPLAY_MAX:
        raise _BadParam("to", "la finestra della moviola è al massimo di 24 ore")
    cursor = None
    if cursor_raw:
        try:
            cursor = decode_cursor(cursor_raw)
        except ValueError:
            raise _BadParam("cursor", "cursor non valido") from None
    events, nxt = await replay_events(session, since=since, until=until, limit=n,
                                      cursor=cursor)
    return {"events": events, "cursor": encode_cursor(*nxt) if nxt else None,
            "from": since, "to": until, "now": now}


@router.get("/flow/events")
async def get_flow_events(session: DbSession, after: str | None = None,
                          limit: str | None = None, cursor: str | None = None,
                          frm: str | None = Query(None, alias="from"),
                          to: str | None = None):
    now = datetime.now(UTC)
    try:
        n = _limit(limit)
        if frm is not None or to is not None:
            if after is not None:
                raise _BadParam("after", "after non va con from e to")
            return await _replay(session, frm, to, cursor, n, now)
        if cursor is not None:
            raise _BadParam("cursor", "cursor va con from e to")
        since = _time(after)
    except _BadParam as e:
        return _unprocessable(e)
    events = await flow_events(session, after=since, limit=n, now=now)
    latest = max((e["ts"] for e in events), default=None)
    if since is not None and (latest is None or latest < since):
        latest = since  # a late MCP row (older ts) never moves the cursor back
    return {"events": events, "cursor": latest or now, "now": now}


@router.get("/flow/summary")
async def get_flow_summary(session: DbSession, window: str | None = None):
    try:
        w = _window(window)
    except _BadParam as e:
        return _unprocessable(e)
    return await flow_summary(session, window=w, now=datetime.now(UTC))


@router.get("/flow/tools")
async def get_flow_tools(session: DbSession, window: str | None = None):
    try:
        w = _window(window)
    except _BadParam as e:
        return _unprocessable(e)
    return await flow_tools(session, window=w, now=datetime.now(UTC))


@router.get("/flow/order/{order_id}")
async def get_flow_order(session: DbSession, order_id: str):
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        return _unprocessable(_BadParam("order_id", "order_id: serve un id d'ordine"))
    story = await order_story(session, oid)
    if story is None:
        return _not_found("ordine non trovato")
    return to_jsonable(story)


@router.get("/flow/mcp/{audit_id}")
async def get_flow_mcp(session: DbSession, audit_id: str):
    try:
        aid = int(audit_id)
        if not 1 <= aid <= AUDIT_ID_MAX:
            raise ValueError
    except ValueError:
        return _unprocessable(_BadParam("audit_id", "audit_id: serve un numero intero"))
    story = await mcp_story(session, aid)
    if story is None:
        return _not_found("chiamata non trovata")
    return to_jsonable(story)


@router.get("/flow/agents")
async def get_flow_agents(session: DbSession):
    return to_jsonable(await flow_agents(session))


@router.get("/flow/health")
async def get_flow_health(session: DbSession, redis: RedisClient):
    return to_jsonable(await flow_health(session, redis))


@router.get("/flow/sources")
async def get_flow_sources(session: DbSession, redis: RedisClient):
    return to_jsonable(await price_sources(session, redis))
