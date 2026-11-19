"""Admin API «Resoconto»: the report of a period (`reports.period`) as JSON or as a PDF
(`reports.pdf`). Read-only: a viewer can download it.

- `GET /api/reports/period?from=AAAA-MM-GG&to=AAAA-MM-GG&portfolio_ids=id,id`: the data
  (numbers as strings, null when unknown). `to` defaults to today (local), `from` to 29
  days before `to`; `portfolio_ids` (comma separated or repeated) defaults to every
  non-archived portfolio (archived ones may be named).
- `GET /api/reports/period.pdf?...`: the same as a PDF download.
The campaign shortcut is `GET /api/campaigns/{campaign_id}/report.pdf` (routes_campaigns)."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from smtrading.admin.auth import DbSession, require_access
from smtrading.admin.common import Quotes, _invalid
from smtrading.common.config import get_settings
from smtrading.mcp.dto import to_jsonable
from smtrading.reports.pdf import filename, render_pdf
from smtrading.reports.period import period_report

router = APIRouter(prefix="/api", tags=["reports"], dependencies=[Depends(require_access)])
From = Annotated[str | None, Query(alias="from")]
PortfolioIds = Annotated[list[str] | None, Query()]
DEFAULT_DAYS = 29


def _now() -> datetime:
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def _day(value: str | None, field: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise _invalid(field, f"{field}: data non valida (AAAA-MM-GG)") from None


def parse_ids(values: list[str] | None) -> list[UUID] | None:
    if not values:
        return None
    out: list[UUID] = []
    for raw in values:
        for part in raw.split(","):
            if part.strip():
                try:
                    pid = UUID(part.strip())
                except ValueError:
                    raise _invalid("portfolio_ids", "portafoglio non valido") from None
                if pid not in out:
                    out.append(pid)
    return out or None


def pdf_response(data: dict[str, Any], body: bytes) -> Response:
    return Response(body, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename(data)}"',
        "Cache-Control": "no-store"})


async def _report(session, qs, from_: str | None, to: str | None,
                  portfolio_ids: list[str] | None) -> dict[str, Any]:
    now, zone = _now(), _zone()
    end = _day(to, "to") or now.astimezone(zone).date()
    start = _day(from_, "from") or end - timedelta(days=DEFAULT_DAYS)
    return await period_report(session, qs, start=start, end=end,
                               portfolio_ids=parse_ids(portfolio_ids), now=now, zone=zone)


@router.get("/reports/period")
async def report_data(session: DbSession, qs: Quotes,
                      from_: From = None, to: str | None = None,
                      portfolio_ids: PortfolioIds = None) -> dict[str, Any]:
    return to_jsonable(await _report(session, qs, from_, to, portfolio_ids))


@router.get("/reports/period.pdf")
async def report_pdf(session: DbSession, qs: Quotes,
                     from_: From = None, to: str | None = None,
                     portfolio_ids: PortfolioIds = None) -> Response:
    data = await _report(session, qs, from_, to, portfolio_ids)
    return pdf_response(data, await asyncio.to_thread(render_pdf, data))
