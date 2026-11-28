"""The TV screens' API, opened by a user's display link only (no portal session, no
password). The TV page is `/tv#token=<link>`: the fragment never reaches a server (nor
its access logs); the page posts it once to `/session`, which checks it and sets the
`smt_tv` cookie (HttpOnly, Secure, SameSite=Strict, path /api/tv-display). Every other
route accepts only that cookie (a `?token=` query is ignored). Read only; the data that
user sees in the portal, nothing else. A missing, revoked or foreign link is 401 at once
(each request checks the stored link, also behind a cookie set before).

    POST /api/tv-display/session {token}   {"ok": true} + the cookie (401 if invalid)

    GET /api/tv-display/selection?after=   {screen, rotation, interval_s, version,
                                           screens, updated_at, poll_s, now, sound,
                                           sound_events, sound_cursor}
    GET /api/tv-display/flow?after=        the river (admins only, like its page)
    GET /api/tv-display/campaign           campaign, equity, goal, portfolios, positions,
                                           fills
    GET /api/tv-display/health             health sections, nodes (admins), alerts
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from smtrading.admin.auth import DbSession, RedisClient
from smtrading.admin.models import AdminUser
from smtrading.common.errors import ErrorCode
from smtrading.flow.health import flow_health
from smtrading.mcp.dto import to_jsonable
from smtrading.tv import display, link, sounds
from smtrading.tv import service as tv
from smtrading.tv.models import TvDisplay

router = APIRouter(prefix="/api/tv-display", tags=["tv-display"])

COOKIE = "smt_tv"
COOKIE_MAX_AGE_S = 400 * 24 * 3600
OVERLAP_S = 10
INVALID_LINK = "link della TV non valido o revocato: apri di nuovo l'indirizzo dal portale"


class DisplayRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


def refused_response(e: DisplayRefused) -> JSONResponse:
    """Also the app's handler of DisplayRefused (api.main)."""
    return JSONResponse({"code": str(ErrorCode.FORBIDDEN), "message": e.message,
                         "details": {}}, status_code=e.status)


class SessionIn(BaseModel):
    token: str = Field(max_length=link.TOKEN_MAX)


async def _owner(request: Request, session: DbSession) -> tuple[TvDisplay, AdminUser]:
    owner = await tv.display_owner(session, request.cookies.get(COOKIE))
    if owner is None:
        raise DisplayRefused(401, INVALID_LINK)
    return owner


Owner = Annotated[tuple[TvDisplay, AdminUser], Depends(_owner)]


def _time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(raw.strip().replace(" ", "+"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


async def _sound_events(session, redis, admin: bool, after: datetime | None,
                        now: datetime) -> list[dict]:
    """Fills and health transitions since `after` (minus OVERLAP_S: the page drops the ids
    it has seen). Commits (the shared health incidents)."""
    if after is None:  # the first poll: nothing from the past
        return []
    since = max(after - timedelta(seconds=OVERLAP_S), now - sounds.LOOKBACK)
    events = await sounds.fill_events(session, since, now)
    sysh = await display.system_health_cached(session, redis, now)
    nodes = None
    if admin:
        fh = await flow_health(session, redis)
        nodes = fh["nodes"]
    state = await sounds.update_incidents(session, sysh["health"], nodes, now)
    events += sounds.health_events(state, since, now, admin=admin)
    events.sort(key=lambda e: (e["at"], e["id"]))
    return events[-sounds.EVENTS_MAX:]


@router.post("/session")
async def open_session(body: SessionIn, response: Response, session: DbSession) -> dict:
    """The TV page's first call: the link from the URL fragment becomes the cookie."""
    if await tv.display_owner(session, body.token) is None:
        raise DisplayRefused(401, INVALID_LINK)
    response.set_cookie(COOKIE, body.token, max_age=COOKIE_MAX_AGE_S, httponly=True,
                        secure=True, samesite="strict", path="/api/tv-display")
    return {"ok": True}


@router.get("/selection")
async def selection(owner: Owner, session: DbSession, redis: RedisClient,
                    after: str | None = None) -> dict:
    d, user = owner
    now = datetime.now(UTC)
    out = {**tv.display_dto(d, user), "screens": tv.allowed_screens(user),
           "poll_s": tv.POLL_S, "now": now, "sound": sounds.settings_dto(d, now)}
    del out["portal_url"]
    events = []
    if d.sound_enabled:
        events = await _sound_events(session, redis, user.is_admin, _time(after), now)
    out.update(sound_events=events, sound_cursor=now)
    return to_jsonable(out)


@router.get("/flow")
async def flow(owner: Owner, session: DbSession, redis: RedisClient,
               after: str | None = None):
    _, user = owner
    if not user.is_admin:
        return refused_response(DisplayRefused(403, "il Fiume dei flussi è solo per gli "
                                              "amministratori"))
    return to_jsonable(await display.flow_screen(session, redis, after=_time(after),
                                                 now=datetime.now(UTC)))


@router.get("/campaign")
async def campaign(owner: Owner, session: DbSession) -> dict:
    return to_jsonable(await display.campaign_screen(session, now=datetime.now(UTC)))


@router.get("/health")
async def health(owner: Owner, session: DbSession, redis: RedisClient) -> dict:
    _, user = owner
    return to_jsonable(await display.health_screen(session, redis, now=datetime.now(UTC),
                                                   with_nodes=user.is_admin))
