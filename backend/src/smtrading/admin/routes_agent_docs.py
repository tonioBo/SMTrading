"""Admin API «Istruzioni agenti»: read, edit (a new version each time), versions, restore.

Session + CSRF like every admin route. PUT carries the `base_version` the editor started
from: when someone saved in between, 409 and nothing is written. Each change is written to
the audit log (`admin:agent_doc.update` / `admin:agent_doc.restore`).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _transaction
from smtrading.admin.idempotency import Idempotency, idempotent
from smtrading.agentdocs import service
from smtrading.agents.service import write_audit

router = APIRouter(prefix="/api/agent-docs", tags=["agent-docs"],
                   dependencies=[Depends(require_admin)])

DocIdem = Annotated[Idempotency, Depends(idempotent("agent-docs"))]


class AgentDocIn(BaseModel):
    body: str
    note: str | None = None
    base_version: int


def _conflict(exc: service.VersionConflict) -> JSONResponse:
    return JSONResponse(
        {"code": "VERSION_CONFLICT", "message": str(exc),
         "details": {"current_version": exc.current_version}},
        status_code=409,
    )


@router.get("")
async def list_agent_docs(session: DbSession) -> list[dict[str, Any]]:
    return await service.list_docs(session)


@router.get("/{slug}")
async def get_agent_doc(slug: str, session: DbSession) -> dict[str, Any]:
    return await service.get_doc(session, slug)


@router.get("/{slug}/versions")
async def list_agent_doc_versions(slug: str, session: DbSession) -> list[dict[str, Any]]:
    return await service.list_versions(session, slug)


@router.get("/{slug}/versions/{version}")
async def get_agent_doc_version(slug: str, version: int, session: DbSession) -> dict[str, Any]:
    return await service.get_version(session, slug, version)


@router.put("/{slug}")
async def save_agent_doc(
    slug: str, body: AgentDocIn, admin: CurrentAdmin, session: DbSession, redis: RedisClient,
    idem: DocIdem,
):
    async def run() -> dict[str, Any]:
        async with _transaction(session, redis):
            doc, changed = await service.save_doc(
                session, slug, body=body.body, note=body.note, base_version=body.base_version,
                author=admin.email,
            )
            if changed:
                await write_audit(
                    session, agent_id=None, method="admin:agent_doc.update",
                    params={"slug": slug, "version": doc["current_version"],
                            "note": service.clean_note(body.note), "by": admin.email},
                    outcome="ok", latency_ms=0,
                )
        return {"doc": doc, "changed": changed}

    try:
        return await idem.run(run)
    except service.VersionConflict as exc:
        return _conflict(exc)


@router.post("/{slug}/restore/{version}")
async def restore_agent_doc(
    slug: str, version: int, admin: CurrentAdmin, session: DbSession, redis: RedisClient,
    idem: DocIdem,
):
    async def run() -> dict[str, Any]:
        async with _transaction(session, redis):
            doc = await service.restore_version(session, slug, version, author=admin.email)
            await write_audit(
                session, agent_id=None, method="admin:agent_doc.restore",
                params={"slug": slug, "version": doc["current_version"],
                        "restored_from": version, "by": admin.email},
                outcome="ok", latency_ms=0,
            )
        return {"doc": doc}

    return await idem.run(run)
