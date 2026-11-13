"""Admin authentication: argon2 passwords, optional TOTP, Redis sessions, CSRF double-submit.

Sessions live in Redis at `adm:sess:{id}` as JSON {admin_id, csrf, created_at} with a 12h
TTL renewed on every authenticated request. The session id travels in the httpOnly
`smt_session` cookie; the CSRF token in the JS-readable `smt_csrf` cookie, which the web
app echoes in `X-CSRF-Token` on every non-GET/HEAD/OPTIONS request.
"""

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.errors import NotAuthenticated
from smtrading.admin.models import AdminUser
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError

SESSION_COOKIE = "smt_session"
CSRF_COOKIE = "smt_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL_S = 12 * 3600
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_S = 15 * 60
MIN_PASSWORD_LEN = 10
TOTP_ISSUER = "SMTrading"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_hasher = PasswordHasher()  # argon2id defaults
_DUMMY_HASH = _hasher.hash("smt-dummy-admin-password")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _session_key(session_id: str) -> str:
    return f"adm:sess:{session_id}"


def _fail_key(email: str) -> str:
    return f"adm:login_fail:{normalize_email(email)}"


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _bad_credentials() -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, "credenziali non valide")


def _bad_totp() -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, "codice TOTP non valido")


def _totp_secret(user: AdminUser) -> str | None:
    if user.totp_secret_encrypted is None:
        return None
    return decrypt_json(user.totp_secret_encrypted, get_settings().master_key)["secret"]


def _totp_ok(secret: str | None, code: str | None) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


async def _record_failure(redis: Redis, email: str) -> None:
    key = _fail_key(email)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, LOGIN_FAIL_WINDOW_S, nx=True)  # window starts at the first failure
        await pipe.execute()


async def create_session(redis: Redis, admin_id: uuid.UUID) -> tuple[str, str]:
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    value = {"admin_id": str(admin_id), "csrf": csrf, "created_at": datetime.now(UTC).isoformat()}
    await redis.set(_session_key(session_id), json.dumps(value), ex=SESSION_TTL_S)
    return session_id, csrf


async def destroy_session(redis: Redis, session_id: str) -> None:
    await redis.delete(_session_key(session_id))


async def login(
    session: AsyncSession, redis: Redis, email: str, password: str, totp: str | None
) -> tuple[str, str]:
    """Return (session_id, csrf). Does not commit (sets `last_login_at`)."""
    fails = await redis.get(_fail_key(email))
    if fails is not None and int(fails) >= LOGIN_FAIL_LIMIT:
        ttl = await redis.ttl(_fail_key(email))
        raise SMTError(
            ErrorCode.RATE_LIMITED, "troppi tentativi di accesso, riprova più tardi",
            {"retry_after_s": max(ttl, 1)},
        )
    user = await session.scalar(select(AdminUser).where(AdminUser.email == normalize_email(email)))
    # an unknown email still costs one argon2 verify: no timing oracle on which emails exist
    ok = await verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not ok:
        await _record_failure(redis, email)
        raise _bad_credentials()
    if user.totp_enabled:
        if not totp:
            raise SMTError(
                ErrorCode.FORBIDDEN, "codice TOTP richiesto", {"totp_required": True}
            )
        if not _totp_ok(_totp_secret(user), totp):
            await _record_failure(redis, email)
            raise _bad_totp()
    await redis.delete(_fail_key(email))
    user.last_login_at = datetime.now(UTC)
    return await create_session(redis, user.id)


def totp_setup(user: AdminUser) -> tuple[str, str]:
    """Store a new (not yet active) secret; return (secret, otpauth_uri). Does not commit."""
    if user.totp_enabled:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP già attivo", {"field": "totp"})
    secret = pyotp.random_base32()
    user.totp_secret_encrypted = encrypt_json({"secret": secret}, get_settings().master_key)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)
    return secret, uri


def totp_enable(user: AdminUser, code: str) -> None:
    if user.totp_secret_encrypted is None:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP non configurato", {"field": "totp"})
    if not _totp_ok(_totp_secret(user), code):
        raise _bad_totp()
    user.totp_enabled = True


def totp_disable(user: AdminUser, code: str) -> None:
    if not user.totp_enabled:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP non attivo", {"field": "totp"})
    if not _totp_ok(_totp_secret(user), code):
        raise _bad_totp()
    user.totp_enabled = False
    user.totp_secret_encrypted = None


async def change_password(user: AdminUser, current: str, new: str) -> None:
    if not await verify_password(user.password_hash, current):
        raise SMTError(ErrorCode.FORBIDDEN, "password attuale errata")
    if len(new) < MIN_PASSWORD_LEN:
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            f"la nuova password deve avere almeno {MIN_PASSWORD_LEN} caratteri",
            {"field": "new"},
        )
    user.password_hash = await hash_password(new)


# --- FastAPI dependencies ----------------------------------------------------------------


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]


async def require_admin(request: Request, session: DbSession, redis: RedisClient) -> AdminUser:
    """The logged-in admin. Renews the session TTL; checks CSRF on unsafe methods.

    The session id is left on `request.state.admin_session_id` (for logout)."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise NotAuthenticated()
    raw = await redis.getex(_session_key(session_id), ex=SESSION_TTL_S)
    if raw is None:
        raise NotAuthenticated()
    data = json.loads(raw)
    if request.method not in _SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent.encode(), data["csrf"].encode()):
            raise SMTError(ErrorCode.FORBIDDEN, "CSRF")
    user = await session.get(AdminUser, uuid.UUID(data["admin_id"]))
    if user is None:
        await destroy_session(redis, session_id)
        raise NotAuthenticated()
    request.state.admin_session_id = session_id
    return user


CurrentAdmin = Annotated[AdminUser, Depends(require_admin)]
