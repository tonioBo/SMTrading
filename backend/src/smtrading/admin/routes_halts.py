"""Admin API «Blocco aperture»: list, set and revoke trading halts (risk.halts).

Session + CSRF like every admin route. The admin is recorded as "admin:<email>"; each
change is also written to the audit log (`admin:halt.set` / `admin:halt.clear`).
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _transaction
from smtrading.agents.service import write_audit
from smtrading.mcp.dto import to_jsonable
from smtrading.risk import halts

router = APIRouter(prefix="/api", tags=["halts"], dependencies=[Depends(require_admin)])

HISTORY_LIMIT = 50


class HaltIn(BaseModel):
    scope: str = "global"
    portfolio_id: UUID | None = None
    reason: str


async def _dto(session, halt) -> dict[str, Any]:
    return to_jsonable((await halts.halt_dtos(session, [halt]))[0])


@router.get("/halts")
async def list_halts(session: DbSession) -> dict:
    """{active: [...] newest first, history: [...] the last 50 cleared}."""
    return to_jsonable({
        "active": await halts.halt_dtos(session, await halts.active_halts(session)),
        "history": await halts.halt_dtos(session, await halts.halt_history(session, HISTORY_LIMIT)),
    })


@router.post("/halts")
async def set_halt(
    body: HaltIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    async with _transaction(session, redis):
        halt, changed, canceled = await halts.set_halt(
            session, scope=body.scope, portfolio_id=body.portfolio_id, reason=body.reason,
            by=f"admin:{admin.email}",
        )
        if changed:
            await write_audit(
                session, agent_id=None, method="admin:halt.set",
                params={"halt_id": halt.id, "scope": halt.scope,
                        "portfolio_id": str(halt.portfolio_id) if halt.portfolio_id else None,
                        "reason": halt.reason, "by": admin.email, "canceled": canceled},
                outcome="ok", latency_ms=0,
            )
        dto = await _dto(session, halt)
    return {"halt": dto, "changed": changed, "canceled": canceled}


@router.post("/halts/{halt_id}/clear")
async def clear_halt(
    halt_id: int, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    async with _transaction(session, redis):
        halt, changed = await halts.clear_halt_by_id(session, halt_id, by=f"admin:{admin.email}")
        if changed:
            await write_audit(
                session, agent_id=None, method="admin:halt.clear",
                params={"halt_id": halt.id, "by": admin.email}, outcome="ok", latency_ms=0,
            )
        dto = await _dto(session, halt)
    return {"halt": dto, "changed": changed}
