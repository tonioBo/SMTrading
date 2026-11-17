"""Admin API: the goals («Obiettivi»). Reads for every user, writes for admins (router
level). Every figure is a string (null without data).

- `GET /api/goals`: every goal, active first (the Goal of the plan's contract, plus
  `portfolio_name`, `portfolio_archived`);
- `POST /api/goals`: name, horizon, target_pct required; start_date (default today,
  local), end_date (custom only, required there), recurring (default false; never for
  custom), scope (default total), portfolio_id (scope portfolio, not archived), kind
  (default min), active (default true);
- `PATCH /api/goals/{goal_id}`: only the fields sent change, validated as a whole;
- `DELETE /api/goals/{goal_id}`: never deleted, active=false;
- `GET /api/goals/progress`: the progress of the active goals in their current period
  (goals of archived portfolios left out)."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictBool

from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import _transaction
from smtrading.common.config import get_settings
from smtrading.goals import service
from smtrading.goals.progress import active_progress
from smtrading.mcp.dto import to_jsonable

router = APIRouter(prefix="/api", tags=["goals"], dependencies=[Depends(require_access)])


class GoalIn(BaseModel):
    """POST and PATCH (PATCH changes only the fields sent: `model_fields_set`)."""

    name: str | None = None
    horizon: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    recurring: StrictBool | None = None
    scope: str | None = None
    portfolio_id: str | None = None
    target_pct: str | int | None = None
    kind: str | None = None
    active: StrictBool | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def _today() -> date:
    return _now().astimezone(_zone()).date()


async def _dto(session, goal) -> dict[str, Any]:
    by_id = {g["id"]: g for g in await service.list_goals(session)}
    return to_jsonable(by_id[goal.id])


@router.get("/goals")
async def list_goals(session: DbSession) -> list[dict[str, Any]]:
    return to_jsonable(await service.list_goals(session))


@router.get("/goals/progress")
async def goals_progress(session: DbSession) -> list[dict[str, Any]]:
    return to_jsonable(await active_progress(session, _now(), _zone()))


@router.post("/goals")
async def create_goal(body: GoalIn, session: DbSession, redis: RedisClient) -> dict[str, Any]:
    async with _transaction(session, redis):
        goal = await service.create_goal(session, body.model_dump(exclude_unset=True),
                                         today=_today())
    return await _dto(session, goal)


@router.patch("/goals/{goal_id}")
async def patch_goal(goal_id: UUID, body: GoalIn, session: DbSession,
                     redis: RedisClient) -> dict[str, Any]:
    goal = await service.get_goal(session, goal_id)
    async with _transaction(session, redis):
        await service.update_goal(session, goal, body.model_dump(exclude_unset=True),
                                  today=_today())
    return await _dto(session, goal)


@router.delete("/goals/{goal_id}")
async def deactivate_goal(goal_id: UUID, session: DbSession,
                          redis: RedisClient) -> dict[str, Any]:
    goal = await service.get_goal(session, goal_id)
    async with _transaction(session, redis):
        await service.deactivate_goal(session, goal)
    return await _dto(session, goal)
