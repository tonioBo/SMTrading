"""Portal API «Schermo TV»: every user's own reserved area (admins and viewers alike: the
actions never touch trading), plus the admins' list of every TV. Never MCP.

Own area (any active user; only their own rows, another user's TV is a 404):

    GET    /api/tv                              the area: TVs, selection, link, sounds
    GET    /api/tv/screens/{tv_id}/power        on | standby | unreachable | not_samsung
    POST   /api/tv/screens          {host, name?}   add a TV (a private IPv4 address)
    DELETE /api/tv/screens/{tv_id}               forget a TV
    POST   /api/tv/screens/{tv_id}/pair          «Collega la TV» (background, ≤ 30 s)
    POST   /api/tv/screens/{tv_id}/send {screen?, rotation?, interval_s?}
                                                 «Manda alla TV»: the only wake-up
    POST   /api/tv/screens/{tv_id}/power-off     «Spegni lo schermo»
    PUT    /api/tv/display  {screen?, rotation?, interval_s?, portal_url?, sound_enabled?,
                             sound_volume?, quiet_start?, quiet_end?}
                                                 the selection the TV page follows (no TV
                                                 command, never a wake-up) and the sounds
    POST   /api/tv/link/rotate                   a new personal display link (the old one
                                                 stops working at once)
    POST   /api/tv/link/revoke                   no display link (every TV locked out)

Admins only:

    POST   /api/tv/discover  {subnet?}           search the LAN for Samsung TVs
    GET    /api/tv/admin/screens                 every user's TVs (user, name, last use)
    POST   /api/tv/admin/screens/{tv_id}/revoke  forget its pairing and revoke the owner's
                                                 display link

The TV pairing token never appears in a response, an error, the audit or a log."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smtrading.admin.auth import CurrentAdmin, CurrentUser, DbSession, RedisClient, require_user
from smtrading.admin.common import _invalid, _transaction
from smtrading.admin.models import AdminUser
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.tv import discovery, samsung, sounds
from smtrading.tv import service as tv
from smtrading.tv.models import TvScreen

router = APIRouter(prefix="/api", tags=["tv"], dependencies=[Depends(require_user)])

_LOCAL_HOSTS = ("localhost", "127.", "[::1]", "::1", "0.0.0.0")


class AddTvIn(BaseModel):
    host: str
    name: str | None = None


class SelectionIn(BaseModel):
    """Only the fields present are changed (`model_fields_set`)."""

    screen: str | None = None
    rotation: bool | None = None
    interval_s: int | None = None


class DisplayIn(SelectionIn):
    portal_url: str | None = None
    sound_enabled: bool | None = None
    sound_volume: int | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None


class DiscoverIn(BaseModel):
    subnet: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def request_base(request: Request) -> str | None:
    """The portal address as the user's browser reaches it (nginx forwards Host and the
    scheme), unless it is a local-only one the TV could never open."""
    host = request.headers.get("host") or ""
    if not host or host.lower().startswith(_LOCAL_HOSTS):
        return None
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def _base(request: Request, portal_url: str | None) -> str | None:
    return portal_url or request_base(request)


def _host_only(request: Request) -> str | None:
    return (request.headers.get("host") or "").rsplit(":", 1)[0] or None


async def _audit(session, method: str, user: AdminUser, **params: Any) -> None:
    await write_audit(session, agent_id=None, method=f"admin:tv.{method}",
                      params={"by": user.email, **params}, outcome="ok", latency_ms=0)


async def _area(session, request: Request, user: AdminUser) -> dict[str, Any]:
    d = await tv.get_display(session, user.id)
    base = _base(request, d.portal_url)
    now = _now()
    subnet = discovery.default_subnet(_host_only(request)) if user.is_admin else None
    out = {
        "screens": tv.screens_dto(user),
        "tvs": [tv.tv_dto(t, now) for t in await tv.user_tvs(session, user.id)],
        "display": tv.display_dto(d, user),
        "sound": sounds.settings_dto(d, now),
        "link": tv.link_dto(d, base),
        "portal_url_detected": request_base(request),
        "can_discover": user.is_admin,
        "suggested_subnet": str(subnet) if subnet else None,
        "interval": {"min": tv.INTERVAL_MIN, "max": tv.INTERVAL_MAX},
        "network_note": tv.NETWORK_NOTE,
    }
    await session.commit()  # the display row, created on the first visit
    return out


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise SMTError(ErrorCode.NOT_FOUND, "TV non trovata") from None


# -- own area ----------------------------------------------------------------------------


@router.get("/tv")
async def get_tv(request: Request, user: CurrentUser, session: DbSession) -> dict:
    return await _area(session, request, user)


@router.get("/tv/screens/{tv_id}/power")
async def get_power(tv_id: str, user: CurrentUser, session: DbSession) -> dict:
    t = await tv.own_tv(session, user.id, _uuid(tv_id))
    host = t.host
    await session.rollback()  # no transaction open while the TV is asked
    return {"power": await tv.power_state(host)}


@router.post("/tv/screens")
async def add_tv(body: AddTvIn, request: Request, user: CurrentUser, session: DbSession,
                 redis: RedisClient) -> dict:
    host = tv.clean_host(body.host)
    name = (body.name or "").strip()[:128] or None
    model = mac = None
    try:
        dev = await samsung.device_info(host)
    except samsung.TvError:
        dev = None  # off or unplugged: saved anyway, paired later
    else:
        if dev is None:
            raise _invalid("host", tv.NOT_SAMSUNG)
    if dev is not None:
        name, model, mac = name or dev["name"][:128], dev["model"], dev["mac"]
    try:
        async with _transaction(session, redis):
            session.add(TvScreen(user_id=user.id, host=host, name=name or "TV",
                                 model=model, mac=mac, pairing_state="idle"))
            await session.flush()
            await _audit(session, "add", user, host=host)
    except IntegrityError:
        raise _invalid("host", "questa TV è già nella tua area") from None
    return await _area(session, request, user)


@router.delete("/tv/screens/{tv_id}")
async def delete_tv(tv_id: str, request: Request, user: CurrentUser, session: DbSession,
                    redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        t = await tv.own_tv(session, user.id, _uuid(tv_id), lock=True)
        await session.delete(t)
        await _audit(session, "delete", user, tv_id=str(t.id))
    return await _area(session, request, user)


@router.post("/tv/screens/{tv_id}/pair")
async def pair_tv(tv_id: str, request: Request, user: CurrentUser, session: DbSession,
                  redis: RedisClient) -> dict:
    """The TV shows «Consentire?»; the outcome arrives on `GET /api/tv` (pairing.state
    waiting → ok | refused | timeout | error). Pairing again replaces the token."""
    now = _now()
    async with _transaction(session, redis):
        t = await tv.own_tv(session, user.id, _uuid(tv_id), lock=True)
        if tv.pairing_state(t, now) == "waiting":
            raise _invalid("tv", "collegamento già in corso: accetta sulla TV")
        t.pairing_state, t.pairing_message, t.pairing_started_at = (
            "waiting", "accetta sulla TV col telecomando", now)
        host, tid = t.host, t.id
        await _audit(session, "pair", user, tv_id=str(tid))
    tv.start_pairing(request.app.state.sessionmaker, tid, host)
    return await _area(session, request, user)


def _apply_selection(d, body: SelectionIn, user: AdminUser) -> bool:
    fields = body.model_fields_set
    changed = False
    if "screen" in fields and body.screen is not None:
        d.screen = tv.clean_screen(body.screen, user)
        changed = True
    if "rotation" in fields and body.rotation is not None:
        d.rotation = body.rotation
        changed = True
    if "interval_s" in fields and body.interval_s is not None:
        d.interval_s = tv.clean_interval(body.interval_s)
        changed = True
    if changed:
        d.version += 1
        d.updated_at = _now()
    return changed


@router.post("/tv/screens/{tv_id}/send")
async def send_tv(tv_id: str, body: SelectionIn, request: Request, user: CurrentUser,
                  session: DbSession, redis: RedisClient) -> dict:
    """Wake the TV if in standby (only here), then open its browser on the user's display
    link. `{ok, message, url, powered_on, app_id, confirmed}`: when `ok` is false or
    `confirmed` false, `url` is the address to open by hand (QR)."""
    async with _transaction(session, redis):
        t = await tv.own_tv(session, user.id, _uuid(tv_id))
        token = tv.decrypt_token(t.token_encrypted)
        if token is None:
            raise _invalid("tv", tv.NOT_PAIRED)
        d = await tv.get_display(session, user.id, lock=True)
        _apply_selection(d, body, user)
        tv.ensure_link(d)
        base = _base(request, d.portal_url)
        problem = tv.url_problem(base)
        if problem is not None:
            raise _invalid("portal_url", problem)  # nothing changed, the TV not contacted
        url = tv.display_url(base, d)
        host, mac, tid = t.host, t.mac, t.id
    result, new_token = await tv.send(host, token, mac, url)
    async with _transaction(session, redis):
        t = await tv.own_tv(session, user.id, tid, lock=True)
        d = await tv.get_display(session, user.id)
        if new_token and new_token != token:
            t.token_encrypted = tv.encrypt_token(new_token)
        t.last_used_at, t.last_ok, t.last_message = _now(), result["ok"], result["message"]
        tv.record_event(session, user.id, tid, "send", d, result["ok"])
        await _audit(session, "send", user, tv_id=str(tid), ok=result["ok"],
                     screen=d.screen, rotation=d.rotation)
    return {"ok": result["ok"], "message": result["message"], "url": url,
            "powered_on": result["powered_on"], "app_id": result["app_id"],
            "confirmed": result["confirmed"]}


@router.post("/tv/screens/{tv_id}/power-off")
async def power_off_tv(tv_id: str, user: CurrentUser, session: DbSession,
                       redis: RedisClient) -> dict:
    t = await tv.own_tv(session, user.id, _uuid(tv_id))
    token = tv.decrypt_token(t.token_encrypted)
    if token is None:
        raise _invalid("tv", tv.NOT_PAIRED)
    host, tid = t.host, t.id
    await session.rollback()
    result, new_token = await tv.power_off(host, token)
    async with _transaction(session, redis):
        t = await tv.own_tv(session, user.id, tid, lock=True)
        if new_token and new_token != token:
            t.token_encrypted = tv.encrypt_token(new_token)
        t.last_used_at, t.last_ok, t.last_message = _now(), result["ok"], result["message"]
        tv.record_event(session, user.id, tid, "power_off", None, result["ok"])
        await _audit(session, "power_off", user, tv_id=str(tid), ok=result["ok"])
    return result


@router.put("/tv/display")
async def put_display(body: DisplayIn, request: Request, user: CurrentUser,
                      session: DbSession, redis: RedisClient) -> dict:
    """The selection («cambia schermata», «rotazione») and the sounds: no TV command."""
    fields = body.model_fields_set
    async with _transaction(session, redis):
        d = await tv.get_display(session, user.id, lock=True)
        changed = _apply_selection(d, body, user)
        if "portal_url" in fields:
            d.portal_url = tv.clean_portal_url(body.portal_url)
        if "sound_enabled" in fields and body.sound_enabled is not None:
            d.sound_enabled = body.sound_enabled
        if "sound_volume" in fields and body.sound_volume is not None:
            d.sound_volume = sounds.clean_volume(body.sound_volume)
        if "quiet_start" in fields:
            d.quiet_start = sounds.clean_hhmm(body.quiet_start, "quiet_start")
        if "quiet_end" in fields:
            d.quiet_end = sounds.clean_hhmm(body.quiet_end, "quiet_end")
        if changed:
            tv.record_event(session, user.id, None, "display", d, True)
        await _audit(session, "display", user, fields=sorted(fields))
    return await _area(session, request, user)


@router.post("/tv/link/rotate")
async def rotate_link(request: Request, user: CurrentUser, session: DbSession,
                      redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        d = await tv.get_display(session, user.id, lock=True)
        tv.rotate_link(d)
        await _audit(session, "link_rotate", user)
    return await _area(session, request, user)


@router.post("/tv/link/revoke")
async def revoke_link(request: Request, user: CurrentUser, session: DbSession,
                      redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        d = await tv.get_display(session, user.id, lock=True)
        tv.revoke_link(d)
        await _audit(session, "link_revoke", user)
    return await _area(session, request, user)


# -- admins ------------------------------------------------------------------------------


@router.post("/tv/discover")
async def discover(body: DiscoverIn, request: Request, admin: CurrentAdmin) -> dict:
    """Samsung TVs on a private network of at most 254 addresses: the one given, else the
    /24 of the portal address in use. A few seconds at most."""
    try:
        net = (discovery.parse_subnet(body.subnet) if body.subnet
               else discovery.default_subnet(_host_only(request)))
    except discovery.SubnetError as e:
        raise _invalid("subnet", str(e)) from None
    if net is None:
        raise _invalid("subnet", "rete non riconosciuta: scrivila, per esempio "
                                 "192.168.1.0/24")
    found = await discovery.discover(discovery.scan_hosts(net))
    return {"subnet": str(net), "devices": found}


def _admin_item(t: TvScreen, email: str | None) -> dict[str, Any]:
    return {"id": str(t.id), "user_id": str(t.user_id), "user_email": email,
            "name": t.name, "host": t.host, "model": t.model,
            "paired": t.token_encrypted is not None,
            "paired_at": tv._iso(t.paired_at), "last_used_at": tv._iso(t.last_used_at),
            "last_ok": t.last_ok, "revoked_at": tv._iso(t.revoked_at),
            "revoked_by": t.revoked_by, "created_at": tv._iso(t.created_at)}


@router.get("/tv/admin/screens")
async def admin_screens(admin: CurrentAdmin, session: DbSession) -> dict:
    rows = (await session.execute(
        select(TvScreen, AdminUser.email).join(AdminUser, AdminUser.id == TvScreen.user_id)
        .order_by(AdminUser.email, TvScreen.created_at))).all()
    return {"items": [_admin_item(t, email) for t, email in rows]}


@router.post("/tv/admin/screens/{tv_id}/revoke")
async def admin_revoke(tv_id: str, admin: CurrentAdmin, session: DbSession,
                       redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        t = await session.scalar(
            select(TvScreen).where(TvScreen.id == _uuid(tv_id)).with_for_update())
        if t is None:
            raise SMTError(ErrorCode.NOT_FOUND, "TV non trovata")
        t.token_encrypted, t.paired_at = None, None
        t.pairing_state, t.pairing_message = "idle", "revocata da un amministratore"
        t.revoked_at, t.revoked_by = _now(), admin.email
        d = await tv.get_display(session, t.user_id, lock=True)
        tv.revoke_link(d)
        await _audit(session, "admin_revoke", admin, tv_id=str(t.id), user_id=str(t.user_id))
        email = await session.scalar(select(AdminUser.email).where(AdminUser.id == t.user_id))
    return _admin_item(t, email)
