from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from smtrading.admin import auth
from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient
from smtrading.admin.models import AdminUser
from smtrading.admin.schemas import AdminOut, LoginIn, PasswordChangeIn, TotpCodeIn, TotpSetupOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _out(user: AdminUser) -> AdminOut:
    return AdminOut(email=user.email, totp_enabled=user.totp_enabled)


_COOKIE_OPTS = {"path": "/", "samesite": "lax", "secure": False}  # local http only


@router.post("/login")
async def login(
    body: LoginIn,
    response: Response,
    session: DbSession,
    redis: RedisClient,
) -> AdminOut:
    session_id, csrf = await auth.login(session, redis, body.email, body.password, body.totp)
    await session.commit()
    user = await session.scalar(
        select(AdminUser).where(AdminUser.email == auth.normalize_email(body.email))
    )
    response.set_cookie(auth.SESSION_COOKIE, session_id, httponly=True, **_COOKIE_OPTS)
    response.set_cookie(auth.CSRF_COOKIE, csrf, httponly=False, **_COOKIE_OPTS)
    return _out(user)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: CurrentAdmin,
    redis: RedisClient,
) -> dict:
    await auth.destroy_session(redis, request.state.admin_session_id)
    response.delete_cookie(auth.SESSION_COOKIE, httponly=True, **_COOKIE_OPTS)
    response.delete_cookie(auth.CSRF_COOKIE, httponly=False, **_COOKIE_OPTS)
    return {"ok": True}


@router.get("/me")
async def me(user: CurrentAdmin) -> AdminOut:
    return _out(user)


@router.post("/totp/setup")
async def totp_setup(
    user: CurrentAdmin, session: DbSession
) -> TotpSetupOut:
    secret, uri = auth.totp_setup(user)
    await session.commit()
    return TotpSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/totp/enable")
async def totp_enable(
    body: TotpCodeIn,
    user: CurrentAdmin,
    session: DbSession,
) -> AdminOut:
    auth.totp_enable(user, body.code)
    await session.commit()
    return _out(user)


@router.post("/totp/disable")
async def totp_disable(
    body: TotpCodeIn,
    user: CurrentAdmin,
    session: DbSession,
) -> AdminOut:
    auth.totp_disable(user, body.code)
    await session.commit()
    return _out(user)


@router.post("/password")
async def change_password(
    body: PasswordChangeIn,
    user: CurrentAdmin,
    session: DbSession,
) -> dict:
    await auth.change_password(user, body.current, body.new)
    await session.commit()
    return {"ok": True}
