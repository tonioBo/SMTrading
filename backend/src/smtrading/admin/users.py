"""Portal user management, shared by /api/users and the `users` CLI.

Users are never deleted, only disabled. Every change is serialized by one transaction
advisory lock (`_lock`), under which "at least one active admin" is checked, and written
to the audit log (`admin:user.*`, never a password). Creating an admin, promoting to
admin and reactivating an admin queue a «system» Telegram security notice (no password).
The caller commits, then revokes the target's sessions (`revoke_sessions`) after a
disable, a role change, a password reset or a TOTP reset.

`step_up`: an optional coroutine the caller passes to confirm an admin-sensitive action
(creating an admin, promoting to admin, resetting an admin's password) with the actor's
TOTP; it runs under the lock, once the target is known, before anything changes."""

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import ActivityLevel
from smtrading.admin.auth import (
    generate_temp_password,
    hash_password,
    normalize_email,
    revoke_other_sessions,
)
from smtrading.admin.models import ROLE_ADMIN, ROLES, AdminUser
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.telegram.config import is_ready, load_config
from smtrading.telegram.outbox import enqueue

CLI_ACTOR = "cli"
MAX_EMAIL_LEN = 254
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LOCK_KEY = "admin_users:roles"
LAST_ADMIN_MESSAGE = "deve restare almeno un amministratore attivo"
StepUp = Callable[[], Awaitable[None]] | None


def user_dto(user: AdminUser) -> dict[str, Any]:
    return {
        "id": str(user.id), "email": user.email, "role": user.role,
        "totp_enabled": user.totp_enabled, "must_change_password": user.must_change_password,
        "active": user.active, "disabled_at": user.disabled_at,
        "last_login_at": user.last_login_at, "created_at": user.created_at,
        "created_by": user.created_by,
    }


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _forbidden(message: str) -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, message)


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise _invalid("role", "ruolo non valido: admin o viewer")
    return role


async def _lock(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})


async def _target(session: AsyncSession, user_id: uuid.UUID) -> AdminUser:
    user = await session.get(AdminUser, user_id, with_for_update=True,
                             populate_existing=True)
    if user is None:
        raise SMTError(ErrorCode.NOT_FOUND, "utente non trovato", {"field": "id"})
    return user


async def find_by_email(session: AsyncSession, email: str) -> AdminUser:
    user = await session.scalar(
        select(AdminUser).where(AdminUser.email == normalize_email(email)))
    if user is None:
        raise SMTError(ErrorCode.NOT_FOUND, "utente non trovato", {"email": email})
    return user


async def _keep_an_admin(session: AsyncSession, losing: AdminUser) -> None:
    """`losing` stops being an active admin: refuse when it is the last one (under _lock)."""
    if not (losing.is_admin and losing.active):
        return
    others = await session.scalar(select(func.count()).select_from(AdminUser).where(
        AdminUser.role == ROLE_ADMIN, AdminUser.disabled_at.is_(None),
        AdminUser.id != losing.id))
    if not others:
        raise _invalid("role", LAST_ADMIN_MESSAGE)


def _not_self(target: AdminUser, actor_id: uuid.UUID | None, message: str) -> None:
    if actor_id is not None and target.id == actor_id:
        raise _forbidden(message)


async def _audit(session: AsyncSession, action: str, actor: str, target: AdminUser,
                 **params: Any) -> None:
    await write_audit(session, agent_id=None, method=f"admin:user.{action}",
                      params={"by": actor, "user_id": str(target.id), "email": target.email,
                              **params},
                      outcome="ok", latency_ms=0)


async def _security_notice(session: AsyncSession, text_: str) -> None:
    cfg = await load_config(session)
    if is_ready(cfg):
        await enqueue(session, source="system", category="event",
                      level=str(ActivityLevel.warning), text=text_, cfg=cfg)


def _who(actor: str) -> str:
    return "riga di comando" if actor == CLI_ACTOR else actor


async def list_users(session: AsyncSession) -> list[AdminUser]:
    return list(await session.scalars(select(AdminUser).order_by(AdminUser.email)))


async def create_user(session: AsyncSession, *, email: str, role: str, actor: str,
                      step_up: StepUp = None) -> tuple[AdminUser, str]:
    """(user, temporary password): the password is returned here only, never stored in
    clear; the user must change it at the first login."""
    clean = normalize_email(email)
    if len(clean) > MAX_EMAIL_LEN or not _EMAIL.match(clean):
        raise _invalid("email", "email non valida")
    _check_role(role)
    await _lock(session)
    if await session.scalar(select(AdminUser.id).where(AdminUser.email == clean)) is not None:
        raise _invalid("email", "esiste già un utente con questa email")
    if role == ROLE_ADMIN and step_up is not None:
        await step_up()
    temp = generate_temp_password()
    user = AdminUser(email=clean, password_hash=await hash_password(temp), role=role,
                     must_change_password=True, created_by=actor)
    session.add(user)
    await session.flush()
    await session.refresh(user)
    await _audit(session, "create", actor, user, role=role)
    if role == ROLE_ADMIN:
        await _security_notice(
            session, f"Sicurezza: nuovo amministratore del portale {clean} "
                     f"(creato da {_who(actor)})")
    return user, temp


async def set_role(session: AsyncSession, user_id: uuid.UUID, role: str, *, actor: str,
                   actor_id: uuid.UUID | None = None, step_up: StepUp = None) -> AdminUser:
    _check_role(role)
    await _lock(session)
    user = await _target(session, user_id)
    _not_self(user, actor_id, "non puoi modificare il tuo ruolo")
    if user.role == role:
        return user
    if role == ROLE_ADMIN:
        if step_up is not None:
            await step_up()
    else:
        await _keep_an_admin(session, user)
    old, user.role = user.role, role
    await session.flush()
    await _audit(session, "set_role", actor, user, old=old, new=role)
    if role == ROLE_ADMIN and user.active:
        await _security_notice(
            session, f"Sicurezza: {user.email} è ora amministratore del portale "
                     f"(promosso da {_who(actor)})")
    return user


async def set_active(session: AsyncSession, user_id: uuid.UUID, active: bool, *, actor: str,
                     actor_id: uuid.UUID | None = None) -> AdminUser:
    await _lock(session)
    user = await _target(session, user_id)
    if not active:
        _not_self(user, actor_id, "non puoi disattivare te stesso")
    if user.active == active:
        return user
    if not active:
        await _keep_an_admin(session, user)
    user.disabled_at = None if active else datetime.now(UTC)
    await session.flush()
    await _audit(session, "enable" if active else "disable", actor, user)
    if active and user.is_admin:
        await _security_notice(
            session, f"Sicurezza: amministratore del portale {user.email} riattivato "
                     f"(da {_who(actor)})")
    return user


async def reset_password(session: AsyncSession, user_id: uuid.UUID, *, actor: str,
                         actor_id: uuid.UUID | None = None,
                         step_up: StepUp = None) -> tuple[AdminUser, str]:
    """(user, new temporary password), returned here only; to be changed at the login."""
    await _lock(session)
    user = await _target(session, user_id)
    _not_self(user, actor_id, "per la tua password usa «Cambia password»")
    if user.is_admin and step_up is not None:
        await step_up()
    temp = generate_temp_password()
    user.password_hash = await hash_password(temp)
    user.must_change_password = True
    await session.flush()
    await _audit(session, "reset_password", actor, user)
    return user, temp


async def reset_totp(session: AsyncSession, user_id: uuid.UUID, *, actor: str,
                     actor_id: uuid.UUID | None = None) -> AdminUser:
    await _lock(session)
    user = await _target(session, user_id)
    _not_self(user, actor_id, "per il tuo TOTP usa la sezione Sicurezza")
    user.totp_enabled = False
    user.totp_secret_encrypted = None
    await session.flush()
    await _audit(session, "reset_totp", actor, user)
    return user


async def revoke_sessions(redis: Redis, user_id: uuid.UUID) -> int:
    """Close every session of the user at once."""
    return await revoke_other_sessions(redis, user_id, keep_session_id="")
