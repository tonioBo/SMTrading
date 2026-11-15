"""Admin API: portal users (admins only). The temporary password is in the create and
reset answers only (`Cache-Control: no-store`). Disable, role change, password reset and
TOTP reset close the target's sessions right after the commit. Creating an admin,
promoting to admin and resetting an admin's password need the actor's TOTP code when the
actor has TOTP enabled (step-up, with the failure lockout)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from smtrading.admin import users
from smtrading.admin.auth import (
    CurrentAdmin,
    DbSession,
    RedisClient,
    check_step_up_totp,
    require_admin,
)
from smtrading.admin.common import _transaction
from smtrading.admin.models import AdminUser
from smtrading.admin.schemas import StepUpIn, UserCreateIn, UserRoleIn
from smtrading.common.errors import ErrorCode, SMTError

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])


def _step_up(redis, actor: AdminUser, code: str | None) -> users.StepUp:
    """The actor's TOTP confirmation, when the actor has TOTP enabled."""
    if not actor.totp_enabled:
        return None

    async def confirm() -> None:
        if not code:
            raise SMTError(ErrorCode.FORBIDDEN, "codice TOTP richiesto",
                           {"field": "totp", "totp_required": True})
        try:
            await check_step_up_totp(redis, actor, code)
        except SMTError as e:
            raise SMTError(e.code, e.message, {**(e.details or {}), "field": "totp"}) from None

    return confirm


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("")
async def list_users(session: DbSession) -> list[dict]:
    return [users.user_dto(u) for u in await users.list_users(session)]


@router.post("")
async def create_user(body: UserCreateIn, response: Response, admin: CurrentAdmin,
                      session: DbSession, redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        user, temp = await users.create_user(
            session, email=body.email, role=body.role, actor=admin.email,
            step_up=_step_up(redis, admin, body.totp))
    _no_store(response)
    return {"user": users.user_dto(user), "temp_password": temp}


@router.put("/{user_id}/role")
async def set_role(user_id: UUID, body: UserRoleIn, admin: CurrentAdmin, session: DbSession,
                   redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        user = await users.set_role(session, user_id, body.role, actor=admin.email,
                                    actor_id=admin.id,
                                    step_up=_step_up(redis, admin, body.totp))
    await users.revoke_sessions(redis, user.id)
    return {"user": users.user_dto(user)}


@router.post("/{user_id}/disable")
async def disable_user(user_id: UUID, admin: CurrentAdmin, session: DbSession,
                       redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        user = await users.set_active(session, user_id, False, actor=admin.email,
                                      actor_id=admin.id)
    await users.revoke_sessions(redis, user.id)
    return {"user": users.user_dto(user)}


@router.post("/{user_id}/enable")
async def enable_user(user_id: UUID, admin: CurrentAdmin, session: DbSession,
                      redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        user = await users.set_active(session, user_id, True, actor=admin.email,
                                      actor_id=admin.id)
    return {"user": users.user_dto(user)}


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: UUID, response: Response, admin: CurrentAdmin,
                         session: DbSession, redis: RedisClient,
                         body: StepUpIn | None = None) -> dict:
    async with _transaction(session, redis):
        user, temp = await users.reset_password(
            session, user_id, actor=admin.email, actor_id=admin.id,
            step_up=_step_up(redis, admin, body.totp if body else None))
    await users.revoke_sessions(redis, user.id)
    _no_store(response)
    return {"user": users.user_dto(user), "temp_password": temp}


@router.post("/{user_id}/reset-totp")
async def reset_totp(user_id: UUID, admin: CurrentAdmin, session: DbSession,
                     redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        user = await users.reset_totp(session, user_id, actor=admin.email, actor_id=admin.id)
    await users.revoke_sessions(redis, user.id)
    return {"user": users.user_dto(user)}
