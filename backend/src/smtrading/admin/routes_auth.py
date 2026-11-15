from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis
from sqlalchemy import select

from smtrading.admin import auth
from smtrading.admin.auth import CurrentAdmin, CurrentUser, DbSession, RedisClient, SessionUser
from smtrading.admin.models import AdminUser
from smtrading.admin.schemas import AdminOut, LoginIn, PasswordChangeIn, TotpCodeIn, TotpSetupOut

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
    response.set_cookie(auth.SESSION_COOKIE, session_id, httponly=True, **opts)
    response.set_cookie(auth.CSRF_COOKIE, csrf, httponly=False, **opts)


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
    session_id, csrf = await auth.login(
        session, redis, body.email, body.password, body.totp, entry=auth.request_entry(request)
    )
    await session.commit()
    user = await session.scalar(
        select(AdminUser).where(AdminUser.email == auth.normalize_email(body.email))
    )
    _set_session_cookies(request, response, session_id, csrf)
    return _out(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, redis: RedisClient) -> Response:
    """Best effort, no session or CSRF required: drop the session if any, clear cookies."""
    session_id = request.cookies.get(auth.SESSION_COOKIE)
    if session_id:
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
    request: Request, user: CurrentAdmin, redis: RedisClient
) -> dict:
    """Close every session of this admin except the caller's (admins only)."""
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
    await auth.change_password(user, body.current, body.new)
    await session.commit()
    await _reissue_session(request, response, redis, user)
    return {"ok": True}
