"""Admin API of the «Roadmap» page: read only, for every portal user (admin and viewer),
never MCP.

    GET /api/roadmap   {updated, summary, campaign, go_no_go, stages, glossary} from the
                       curated file (`roadmap.service`) plus `live`: campaign day, equity
                       of the portfolios in the totals against the campaign goal, drills
                       done / planned, days to the «sì/no» decision, Neurograph tools
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from smtrading.admin.auth import DbSession, require_access
from smtrading.mcp.dto import to_jsonable
from smtrading.roadmap.service import roadmap_view

router = APIRouter(prefix="/api", tags=["roadmap"], dependencies=[Depends(require_access)])


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/roadmap")
async def get_roadmap(session: DbSession) -> dict[str, Any]:
    return to_jsonable(await roadmap_view(session, _now()))
