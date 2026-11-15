"""Admin API of the «Registro attività» (admins only, a viewer gets 403):

    GET /api/activity-log           newest first; filters email, category, action, outcome,
                                    from/to (ISO 8601), before_id, limit
    GET /api/activity-log.csv       every matching row: `;`, UTF-8 with BOM, formula cells
                                    neutralized
    GET /api/activity-log/catalog   the categories and actions with their Italian labels

The rows are written by useractivity.recorder; the log is append-only and kept forever.
"""

import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, select

from smtrading.admin.auth import DbSession, require_admin
from smtrading.admin.common import _invalid
from smtrading.admin.routes_portfolios import _csv_text
from smtrading.mcp.dto import parse_int, parse_ts, to_jsonable
from smtrading.useractivity import catalog
from smtrading.useractivity.models import OUTCOMES, UserActivity

router = APIRouter(prefix="/api", tags=["activity-log"], dependencies=[Depends(require_admin)])

DEFAULT_LIMIT, MAX_LIMIT = 50, 200
CSV_BATCH = 500
CSV_HEADER = ("id", "data", "utente", "ruolo", "azione", "categoria", "metodo", "route",
              "obiettivo", "esito", "stato", "codice errore", "messaggio", "ip", "user agent",
              "riepilogo", "id richiesta")
OUTCOME_LABELS = {"ok": "Riuscita", "denied": "Negata", "error": "Errore"}


def _query(email: str | None, category: str | None, action: str | None,
           outcome: str | None, from_: str | None, to: str | None) -> Select:
    q = select(UserActivity).order_by(UserActivity.id.desc())
    if email:
        q = q.where(UserActivity.email == email.strip().lower())
    if category:
        if category not in catalog.CATEGORIES:
            raise _invalid("category", "categoria non valida")
        q = q.where(UserActivity.action.startswith(category + ".", autoescape=True))
    if action:
        q = q.where(UserActivity.action == action)
    if outcome:
        if outcome not in OUTCOMES:
            raise _invalid("outcome", "esito non valido: ok, denied o error")
        q = q.where(UserActivity.outcome == outcome)
    start, end = parse_ts(from_, "from"), parse_ts(to, "to")
    if start is not None:
        q = q.where(UserActivity.ts >= start)
    if end is not None:
        q = q.where(UserActivity.ts < end)
    return q


def _dto(row: UserActivity) -> dict[str, Any]:
    cat = catalog.category(row.action)
    return {
        "id": row.id, "ts": to_jsonable(row.ts), "request_id": str(row.request_id),
        "user_id": to_jsonable(row.user_id), "email": row.email, "role": row.role,
        "action": row.action, "action_label": catalog.label(row.action),
        "category": cat, "category_label": catalog.CATEGORIES.get(cat, cat),
        "method": row.method, "route": row.route, "target": row.target,
        "outcome": row.outcome, "status": row.status, "error_code": row.error_code,
        "error_message": row.error_message, "ip": row.ip, "user_agent": row.user_agent,
        "summary": row.summary,
    }


Filters = dict[str, str | None]


def _filters(email: str | None = None, category: str | None = None, action: str | None = None,
             outcome: str | None = None,
             from_: Annotated[str | None, Query(alias="from")] = None,
             to: str | None = None) -> Filters:
    return {"email": email, "category": category, "action": action, "outcome": outcome,
            "from_": from_, "to": to}


@router.get("/activity-log")
async def get_activity_log(
    session: DbSession, filters: Annotated[Filters, Depends(_filters)],
    before_id: int | None = None, limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """Newest first; `before_id` pages back."""
    n = parse_int(limit, "limit", 1, MAX_LIMIT)
    before = parse_int(before_id, "before_id", 1)
    q = _query(**filters)
    if before is not None:
        q = q.where(UserActivity.id < before)
    return [_dto(r) for r in await session.scalars(q.limit(n))]


def _json_cell(value: Any) -> str:
    return "" if not value else _csv_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _csv_row(r: UserActivity) -> tuple:
    return (
        r.id, to_jsonable(r.ts), _csv_text(r.email), r.role or "",
        _csv_text(catalog.label(r.action)),
        _csv_text(catalog.CATEGORIES.get(catalog.category(r.action), "")),
        r.method, _csv_text(r.route), _json_cell(r.target),
        OUTCOME_LABELS.get(r.outcome, r.outcome), r.status or "", _csv_text(r.error_code),
        _csv_text(r.error_message), _csv_text(r.ip), _csv_text(r.user_agent),
        _json_cell(r.summary), str(r.request_id),
    )


@router.get("/activity-log.csv")
async def get_activity_log_csv(
    request: Request, filters: Annotated[Filters, Depends(_filters)],
) -> StreamingResponse:
    """Every row matching the filters, newest first: `;` separated, UTF-8 with BOM, a cell
    that would start a formula gets a leading '."""
    query = _query(**filters)
    sessionmaker = request.app.state.sessionmaker

    def encode(rows: list[tuple]) -> str:
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(rows)
        return buf.getvalue()

    async def generate() -> AsyncIterator[bytes]:
        yield ("﻿" + encode([CSV_HEADER])).encode()
        before: int | None = None
        async with sessionmaker() as s:  # the request session may be closed by now
            while True:
                q = query if before is None else query.where(UserActivity.id < before)
                batch = list(await s.scalars(q.limit(CSV_BATCH)))
                if not batch:
                    return
                yield encode([_csv_row(r) for r in batch]).encode()
                before = batch[-1].id

    return StreamingResponse(
        generate(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="registro-attivita.csv"'},
    )


@router.get("/activity-log/catalog")
async def get_activity_log_catalog() -> dict:
    return {
        "categories": [{"key": k, "label": v} for k, v in catalog.CATEGORIES.items()],
        "actions": [{"key": k, "label": v, "category": catalog.category(k)}
                    for k, v in catalog.LABELS.items()],
        "outcomes": [{"key": k, "label": v} for k, v in OUTCOME_LABELS.items()],
    }
