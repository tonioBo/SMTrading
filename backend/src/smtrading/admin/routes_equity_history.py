"""Admin API: `GET /api/portfolios/equity-history?range=2h|6h|12h|1d|1w|1m|3m|all[&portfolio_id=]`,
the «Andamento» chart (dashboard: every portfolio that is not archived; portfolio detail:
one portfolio, archived ones too). Read-only, viewer allowed (`require_access`). The
maths are in `performance.history`.

Included before the portfolios router: `/portfolios/{portfolio_id}` would otherwise take
`equity-history` for an id."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query

from smtrading.admin.auth import DbSession, require_access
from smtrading.common.config import get_settings
from smtrading.mcp.dto import to_jsonable
from smtrading.performance.history import equity_history

router = APIRouter(prefix="/api", tags=["portfolios"], dependencies=[Depends(require_access)])


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/portfolios/equity-history")
async def portfolios_equity_history(
    session: DbSession, range_: Annotated[str, Query(alias="range")] = "1m",
    portfolio_id: UUID | None = None,
) -> dict[str, Any]:
    return to_jsonable(await equity_history(
        session, range_, now=_now(), zone=ZoneInfo(get_settings().tz),
        portfolio_id=portfolio_id,
    ))
