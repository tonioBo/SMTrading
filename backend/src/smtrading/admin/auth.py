"""Admin authentication: argon2 passwords, optional TOTP, Redis sessions, CSRF double-submit.

Sessions live in Redis at `adm:sess:{id}` as JSON {admin_id, csrf, created_at, fp} with a
12h idle TTL renewed on every authenticated request, and an absolute lifetime of 24h from
`created_at` whatever the activity. Each admin's session ids are indexed in the set
`adm:sess_idx:{admin_id}` (to close the other sessions). `fp` fingerprints the admin's credentials
(password hash + TOTP state): changing either invalidates every session issued before. The session id travels in the httpOnly
`smt_session` cookie; the CSRF token in the JS-readable `smt_csrf` cookie, which the web
app echoes in `X-CSRF-Token` on every non-GET/HEAD/OPTIONS request.

Roles: `require_user` admits every active user (reads, and the few actions a viewer may
take), `require_admin` only admins; `require_access`, the admin routers' default, admits
every user on reads and only admins on unsafe methods. A disabled user's sessions are
refused (and destroyed); a user with `must_change_password` gets PASSWORD_CHANGE_REQUIRED
everywhere except me, logout and the password change (`require_session_user`).

Step-up TOTP (`check_step_up_totp`): attempts are counted before the code is verified
(`adm:totp_fail:{id}`); 5 wrong codes in 15 minutes lock the user's step-up checks for 15
minutes, a success clears the count.
"""

import asyncio
import hashlib
import json
import logging
import secrets
import string
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
from smtrading.common.logsetup import register_secret
from smtrading.useractivity import context as activity
from smtrading.useractivity.recorder import bind_session

log = logging.getLogger(__name__)

SESSION_COOKIE = "smt_session"
CSRF_COOKIE = "smt_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL_S = 24 * 3600  # idle (user decision 2026-11-15)
SESSION_MAX_AGE_S = 31 * 24 * 3600  # absolute, from created_at (user decision 2026-11-15)
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_S = 15 * 60
MIN_PASSWORD_LEN = 10
TOTP_ISSUER = "SMTrading"
TOTP_USED_TTL_S = 90  # covers valid_window=1 (previous, current, next 30s step)
TOTP_FAIL_LIMIT = 5
TOTP_FAIL_WINDOW_S = 15 * 60  # also the lockout after the 5th failure
TOTP_LOCKED_MESSAGE = "troppi codici TOTP errati, riprova tra 15 minuti"
TEMP_PASSWORD_LEN = 20
# no look-alike characters (0/O, 1/l/I): the password is read off a screen and typed
_TEMP_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
VIEWER_FORBIDDEN_MESSAGE = "azione non consentita: utente in sola lettura"
PASSWORD_CHANGE_MESSAGE = "devi cambiare la password temporanea prima di continuare"
DISABLED_MESSAGE = "utente disattivato"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# nginx tags each request with its entry point: the plain-http local one (127.0.0.1:3000)
# or the LAN one. Login failures are counted per entry, so failures from the LAN cannot
# lock the local entry out. A missing or unknown value counts as the LAN.
ENTRY_HEADER = "X-SMT-Entry"
ENTRY_LOCAL, ENTRY_LAN = "local", "lan"

_hasher = PasswordHasher()  # argon2id defaults
_DUMMY_HASH = _hasher.hash("smt-dummy-admin-password")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _session_key(session_id: str) -> str:
    return f"adm:sess:{session_id}"


def _session_index_key(admin_id) -> str:
    return f"adm:sess_idx:{admin_id}"


def request_entry(request: Request) -> str:
    return ENTRY_LOCAL if request.headers.get(ENTRY_HEADER) == ENTRY_LOCAL else ENTRY_LAN


def _fail_key(email: str, entry: str) -> str:
    return f"adm:login_fail:{entry}:{normalize_email(email)}"


def credentials_fingerprint(user: AdminUser) -> str:
    raw = f"{user.password_hash}|{user.totp_enabled}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


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


def generate_temp_password() -> str:
    """A server-generated temporary password, shown once. Registered with the log filter:
    it is masked in any log text of this process."""
    password = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(TEMP_PASSWORD_LEN))
    register_secret(password)
    return password


async def _check_totp(redis: Redis, user: AdminUser, code: str | None) -> None:
    """Verify `code` against the stored secret; a code is accepted once (replay guard)."""
    if not _totp_ok(_totp_secret(user), code):
        raise _bad_totp()
    used_key = f"adm:totp_used:{user.id}:{code.strip()}"
    if not await redis.set(used_key, "1", nx=True, ex=TOTP_USED_TTL_S):
        raise SMTError(ErrorCode.FORBIDDEN, "codice TOTP già usato")


def _totp_fail_key(user: AdminUser) -> str:
    return f"adm:totp_fail:{user.id}"


async def check_step_up_totp(redis: Redis, user: AdminUser, code: str | None) -> None:
    """`_check_totp` for a step-up (an action confirmed with a code, not the login, which
    has its own limit). Every attempt is counted first, atomically (INCR, then the window
    with EXPIRE NX), and only then verified: beyond TOTP_FAIL_LIMIT attempts within
    TOTP_FAIL_WINDOW_S the code is not even checked (RATE_LIMITED), so parallel requests
    cannot try more than TOTP_FAIL_LIMIT codes. The 5th failure restarts the window: a
    15-minute lockout. A failure is logged (never the code); a success clears the count.
    Redis only."""
    key = _totp_fail_key(user)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, TOTP_FAIL_WINDOW_S, nx=True)
        count, _ = await pipe.execute()
    count = int(count)
    if count > TOTP_FAIL_LIMIT:
        raise SMTError(ErrorCode.RATE_LIMITED, TOTP_LOCKED_MESSAGE)
    if not _totp_ok(_totp_secret(user), code):
        if count == TOTP_FAIL_LIMIT:
            await redis.expire(key, TOTP_FAIL_WINDOW_S)  # a full lockout from this failure
            activity.note_event("auth.totp_lockout")
        log.warning("wrong step-up TOTP code for admin user %s (%d/%d)", user.id, count,
                    TOTP_FAIL_LIMIT)
        raise _bad_totp()
    await redis.delete(key)
    await _check_totp(redis, user, code)  # the replay guard


async def _count_attempt(redis: Redis, email: str, entry: str) -> int:
    """Count this login attempt (the counter is cleared on success); return the count."""
    key = _fail_key(email, entry)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, LOGIN_FAIL_WINDOW_S, nx=True)  # window starts at the first attempt
        count, _ = await pipe.execute()
    return int(count)


async def create_session(redis: Redis, user: AdminUser) -> tuple[str, str]:
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    value = {
        "admin_id": str(user.id),
        "csrf": csrf,
        "created_at": datetime.now(UTC).isoformat(),
        "fp": credentials_fingerprint(user),
    }
    index = _session_index_key(user.id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(_session_key(session_id), json.dumps(value), ex=SESSION_TTL_S)
        pipe.sadd(index, session_id)
        pipe.expire(index, SESSION_MAX_AGE_S)  # outlives every session it lists
        await pipe.execute()
    return session_id, csrf


async def destroy_session(redis: Redis, session_id: str, admin_id=None) -> None:
    """Delete the session and drop it from its admin's index (`admin_id` is read from the
    session when not given)."""
    key = _session_key(session_id)
    if admin_id is None:
        raw = await redis.get(key)
        admin_id = json.loads(raw)["admin_id"] if raw is not None else None
    await redis.delete(key)
    if admin_id is not None:
        await redis.srem(_session_index_key(admin_id), session_id)


async def revoke_other_sessions(redis: Redis, admin_id, keep_session_id: str) -> int:
    """Destroy every session of the admin except `keep_session_id`; return how many."""
    index = _session_index_key(admin_id)
    others = [
        sid for sid in (m.decode() if isinstance(m, bytes) else m
                        for m in await redis.smembers(index))
        if sid != keep_session_id
    ]
    if not others:
        return 0
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(*(_session_key(sid) for sid in others))
        pipe.srem(index, *others)
        deleted, _ = await pipe.execute()
    return int(deleted)


async def login(
    session: AsyncSession, redis: Redis, email: str, password: str, totp: str | None,
    *, entry: str = ENTRY_LAN,
) -> tuple[str, str]:
    """Return (session_id, csrf). Does not commit (sets `last_login_at`).

    Every attempt is counted first (no check-then-increment race); a successful login clears
    the counter, so only failures accumulate: the 6th attempt after 5 failures is refused.
    The counter is per (email, entry point)."""
    if await _count_attempt(redis, email, entry) > LOGIN_FAIL_LIMIT:
        ttl = await redis.ttl(_fail_key(email, entry))
        raise SMTError(
            ErrorCode.RATE_LIMITED, "troppi tentativi di accesso, riprova più tardi",
            {"retry_after_s": max(ttl, 1)},
        )
    user = await session.scalar(select(AdminUser).where(AdminUser.email == normalize_email(email)))
    # an unknown email still costs one argon2 verify: no timing oracle on which emails exist
    ok = await verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not ok:
        raise _bad_credentials()
    if user.disabled_at is not None:  # told only with the right password
        raise SMTError(ErrorCode.FORBIDDEN, DISABLED_MESSAGE)
    if user.totp_enabled:
        if not totp:
            raise SMTError(
                ErrorCode.FORBIDDEN, "codice TOTP richiesto", {"totp_required": True}
            )
        await _check_totp(redis, user, totp)
    await redis.delete(_fail_key(email, entry))
    user.last_login_at = datetime.now(UTC)
    return await create_session(redis, user)


def totp_setup(user: AdminUser) -> tuple[str, str]:
    """Store a new (not yet active) secret; return (secret, otpauth_uri). Does not commit."""
    if user.totp_enabled:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP già attivo", {"field": "totp"})
    secret = pyotp.random_base32()
    user.totp_secret_encrypted = encrypt_json({"secret": secret}, get_settings().master_key)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)
    return secret, uri


async def totp_enable(redis: Redis, user: AdminUser, code: str) -> None:
    if user.totp_enabled:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP già attivo", {"field": "totp"})
    if user.totp_secret_encrypted is None:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP non configurato", {"field": "totp"})
    await check_step_up_totp(redis, user, code)
    user.totp_enabled = True


async def totp_disable(redis: Redis, user: AdminUser, code: str) -> None:
    if not user.totp_enabled:
        raise SMTError(ErrorCode.INVALID_ORDER, "TOTP non attivo", {"field": "totp"})
    await check_step_up_totp(redis, user, code)
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
    if user.must_change_password and new == current:
        raise SMTError(
            ErrorCode.INVALID_ORDER, "la nuova password deve essere diversa da quella temporanea",
            {"field": "new"},
        )
    user.password_hash = await hash_password(new)
    user.must_change_password = False


# --- FastAPI dependencies ----------------------------------------------------------------


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        bind_session(session)  # its first commit carries the activity row (useractivity)
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]


async def load_admin_session(
    redis: Redis, db: AsyncSession, session_id: str | None, *, renew: bool
) -> tuple[AdminUser, dict] | None:
    """(admin, session data) for a live session id, else None. `renew` resets the idle TTL
    (never beyond the absolute lifetime).

    A session past its absolute lifetime, issued before the admin's password or TOTP state
    changed (fingerprint mismatch), or whose admin is gone or disabled, is destroyed and
    gives None."""
    if not session_id:
        return None
    key = _session_key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return None
    data = json.loads(raw)
    admin_id = uuid.UUID(data["admin_id"])
    age = (datetime.now(UTC) - datetime.fromisoformat(data["created_at"])).total_seconds()
    remaining = int(SESSION_MAX_AGE_S - age)
    if remaining <= 0:
        await destroy_session(redis, session_id, admin_id)
        return None
    user = await db.get(AdminUser, admin_id)
    if (user is None or user.disabled_at is not None
            or data.get("fp") != credentials_fingerprint(user)):
        await destroy_session(redis, session_id, admin_id)
        return None
    if renew:
        await redis.expire(key, min(SESSION_TTL_S, remaining))
    return user, data


async def require_session_user(
    request: Request, session: DbSession, redis: RedisClient
) -> AdminUser:
    """The logged-in user, any role, even with a temporary password to change (only for
    me and the password change). Renews the session TTL; checks CSRF on unsafe methods.

    A session issued before the user's password or TOTP state changed, or of a disabled
    user, is revoked (401). The session id is left on `request.state.admin_session_id`."""
    session_id = request.cookies.get(SESSION_COOKIE)
    loaded = await load_admin_session(redis, session, session_id, renew=True)
    if loaded is None:
        raise NotAuthenticated()
    user, data = loaded
    activity.note_actor(user)
    if request.method not in _SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent.encode(), data["csrf"].encode()):
            raise SMTError(ErrorCode.FORBIDDEN, "CSRF")
    request.state.admin_session_id = session_id
    return user


async def require_user(
    user: Annotated[AdminUser, Depends(require_session_user)],
) -> AdminUser:
    """Any active user (admin or viewer) whose password is not a temporary one."""
    if user.must_change_password:
        raise SMTError(ErrorCode.PASSWORD_CHANGE_REQUIRED, PASSWORD_CHANGE_MESSAGE)
    return user


def viewer_forbidden() -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, VIEWER_FORBIDDEN_MESSAGE, {"role_required": "admin"})


async def require_admin(user: Annotated[AdminUser, Depends(require_user)]) -> AdminUser:
    """An admin, whatever the method."""
    if not user.is_admin:
        raise viewer_forbidden()
    return user


async def require_access(
    request: Request, user: Annotated[AdminUser, Depends(require_user)]
) -> AdminUser:
    """The admin routers' default: reads (GET/HEAD/OPTIONS) for every user, unsafe
    methods for admins only."""
    if request.method not in _SAFE_METHODS and not user.is_admin:
        raise viewer_forbidden()
    return user


SessionUser = Annotated[AdminUser, Depends(require_session_user)]
CurrentUser = Annotated[AdminUser, Depends(require_user)]
CurrentAdmin = Annotated[AdminUser, Depends(require_admin)]
