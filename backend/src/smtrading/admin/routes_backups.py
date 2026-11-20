"""Admin API «Backup automatici» (the compose `backup` service, `smtrading.backup`).

- `GET /api/backups` (admin and viewer): the files of every destination (as the service
  last listed them, after its retention), the last backup, the last fully ok one, the
  last restore test, the recent runs, the schedule and a request still waiting.
- `POST /api/backups/run` (admin): asks the service for a backup now (body
  `{"restore_test": true}` also asks for a restore test). The service picks it up within
  its poll interval; 202 with the request. Written to the audit log."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import _transaction
from smtrading.agents.service import write_audit
from smtrading.backup import service
from smtrading.backup.runner import RESTORE_DB
from smtrading.common.config import get_settings
from smtrading.mcp.dto import to_jsonable

router = APIRouter(prefix="/api/backups", tags=["backups"], dependencies=[Depends(require_access)])


class RunIn(BaseModel):
    restore_test: bool = False


def schedule() -> dict:
    return {"tz": get_settings().tz, "backup_at": service.BACKUP_AT.strftime("%H:%M"),
            "restore_test_weekday": service.RESTORE_WEEKDAY,
            "restore_test_at": service.RESTORE_AT.strftime("%H:%M"),
            "restore_test_database": RESTORE_DB,
            "retention": {"daily": service.files.KEEP_DAILY,
                          "monthly": service.files.KEEP_MONTHLY}}


@router.get("")
async def get_backups(session: DbSession) -> dict:
    state = await service.load_state(session)
    return to_jsonable({
        "destinations": state["destinations"], "last_backup": state["last_backup"],
        "last_ok_at": state["last_ok_at"], "last_ok_name": state["last_ok_name"],
        "last_restore_test": state["last_restore_test"], "history": state["history"],
        "pending_request": await service.load_request(session), "schedule": schedule(),
    })


@router.post("/run", status_code=202)
async def run_backup(admin: CurrentAdmin, session: DbSession, redis: RedisClient,
                     body: RunIn | None = None) -> JSONResponse:
    restore_test = bool(body and body.restore_test)
    async with _transaction(session, redis):
        req = await service.request_run(session, by=admin.email, restore_test=restore_test)
        await write_audit(session, agent_id=None, method="admin:backup.run",
                          params={"by": admin.email, "restore_test": restore_test},
                          outcome="ok", latency_ms=0)
    return JSONResponse({"request": to_jsonable(req), "poll_s": service.POLL_S}, status_code=202)
