import logging

from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis
from sqlalchemy import select

from smtrading.admin import auth
from smtrading.admin.auth import CurrentUser, DbSession, RedisClient, SessionUser
from smtrading.admin.models import AdminUser
from smtrading.admin.schemas import AdminOut, LoginIn, PasswordChangeIn, TotpCodeIn, TotpSetupOut
from smtrading.useractivity import context as activity

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _out(user: AdminUser) -> AdminOut:
    return AdminOut(email=user.email, totp_enabled=user.totp_enabled, role=user.role,
                    must_change_password=user.must_change_password)


def _cookie_opts(request: Request) -> dict:
    """`Secure` when the browser came over https. Behind nginx the scheme is its
    `X-Forwarded-Proto`, applied by uvicorn's proxy-headers middleware only for the
    trusted compose subnet (docker/entrypoint-api.sh); the local entry is plain http."""
    return {"path": "/", "samesite": "lax", "secure": request.url.scheme == "https"}


def _set_session_cookies(
    request: Request, response: Response, session_id: str, csrf: str
) -> None:
    opts = _cookie_opts(request)
    # persistent cookies (survive a browser restart); the server-side session still
    # enforces the idle and absolute limits
    response.set_cookie(auth.SESSION_COOKIE, session_id, httponly=True,
                        max_age=auth.SESSION_MAX_AGE_S, **opts)
    response.set_cookie(auth.CSRF_COOKIE, csrf, httponly=False,
                        max_age=auth.SESSION_MAX_AGE_S, **opts)


async def _reissue_session(
    request: Request, response: Response, redis: Redis, user: AdminUser
) -> None:
    """After a credential change (already committed): every older session, this one
    included, is destroyed right away (not only invalidated by the fingerprint, which a
    later change back could match again); give the caller a fresh one."""
    await auth.destroy_session(redis, request.state.admin_session_id, user.id)
    await auth.revoke_other_sessions(redis, user.id, keep_session_id="")
    session_id, csrf = await auth.create_session(redis, user)
    _set_session_cookies(request, response, session_id, csrf)


@router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    session: DbSession,
    redis: RedisClient,
) -> AdminOut:
    activity.note_email(auth.normalize_email(body.email))  # a failure records the attempt
    session_id, csrf = await auth.login(
        session, redis, body.email, body.password, body.totp, entry=auth.request_entry(request)
    )
    user = await session.scalar(
        select(AdminUser).where(AdminUser.email == auth.normalize_email(body.email))
    )
    activity.note_actor(user)
    await session.commit()
    _set_session_cookies(request, response, session_id, csrf)
    return _out(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, session: DbSession, redis: RedisClient) -> Response:
    """Best effort, no session or CSRF required: drop the session if any, clear cookies.
    The session's user, if still valid, is who logs out in the activity log."""
    session_id = request.cookies.get(auth.SESSION_COOKIE)
    if session_id:
        try:
            loaded = await auth.load_admin_session(redis, session, session_id, renew=False)
        except Exception:  # who it was is only for the log: the logout still happens
            log.warning("logout: the session's user could not be read", exc_info=True)
            loaded = None
        if loaded is not None:
            activity.note_actor(loaded[0])
        await auth.destroy_session(redis, session_id)
    response = Response(status_code=204)
    opts = _cookie_opts(request)
    response.delete_cookie(auth.SESSION_COOKIE, httponly=True, **opts)
    response.delete_cookie(auth.CSRF_COOKIE, httponly=False, **opts)
    return response


@router.get("/me")
async def me(user: SessionUser) -> AdminOut:
    return _out(user)


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    request: Request, user: CurrentUser, redis: RedisClient
) -> dict:
    """Close every session of this user except the caller's (any role: it only protects
    the caller's own account)."""
    revoked = await auth.revoke_other_sessions(redis, user.id, request.state.admin_session_id)
    return {"revoked": revoked}


@router.post("/totp/setup")
async def totp_setup(
    user: CurrentUser, session: DbSession
) -> TotpSetupOut:
    secret, uri = auth.totp_setup(user)
    await session.commit()
    return TotpSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/totp/enable")
async def totp_enable(
    body: TotpCodeIn,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    redis: RedisClient,
) -> AdminOut:
    await auth.totp_enable(redis, user, body.code)
    await session.commit()
    await _reissue_session(request, response, redis, user)
    return _out(user)


@router.post("/totp/disable")
async def totp_disable(
    body: TotpCodeIn,
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    redis: RedisClient,
) -> AdminOut:
    await auth.totp_disable(redis, user, body.code)
    await session.commit()
    await _reissue_session(request, response, redis, user)
    return _out(user)


@router.post("/password")
async def change_password(
    body: PasswordChangeIn,
    request: Request,
    response: Response,
    user: SessionUser,
    session: DbSession,
    redis: RedisClient,
) -> dict:
    forced = user.must_change_password
    await auth.change_password(user, body.current, body.new)
    if forced:
        activity.set_action("auth.password_forced_change")
    await session.commit()
    await _reissue_session(request, response, redis, user)
    return {"ok": True}
