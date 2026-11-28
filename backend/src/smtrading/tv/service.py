"""«Schermo TV»: what the portal routes do, per user (each user only ever reaches their
own rows: `own_tv` / `get_display` filter on the user id).

- A TV (`tv_screens`): an address of the user's home network, paired once
  (`start_pairing`: in the background, the TV asks «Consentire?» and waits up to
  PAIR_WAIT_S; the page polls `GET /api/tv`). Pairing again always starts without the
  old token and replaces it. A user may add any address of the network, also a TV that
  another user paired: nothing is shared (each pairing has its own token) and the TV
  itself still asks for consent on screen («Consentire?»), so nobody drives a TV
  without someone accepting with its remote.
- «Manda alla TV» (`send`): the only action that wakes a TV in standby (KEY_POWER, then a
  Wake-on-LAN packet when the TV gives its MAC), then the TV browser opens the user's
  display link; everything within SEND_TIMEOUT_S. A screen change or the rotation only
  changes the selection the TV page polls: never a TV command, never a wake-up.
- The display link (`tv.link`) and the selection (`tv_displays`).

The pairing token is encrypted (aad=b"tv-token"), registered with the log filter when
read, and never returned; TV failures are plain Italian (`samsung.TvError.message`)."""

import asyncio
import ipaddress
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.admin.common import _invalid
from smtrading.admin.models import AdminUser
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import register_secret
from smtrading.tv import discovery, samsung
from smtrading.tv import link as tvlink
from smtrading.tv.models import TvDisplay, TvEvent, TvScreen

log = logging.getLogger(__name__)

TOKEN_AAD = b"tv-token"
PAIR_WAIT_S = 30.0  # the owner walks to the TV and accepts with the remote
PAIR_STALE_S = 15.0  # a «waiting» older than PAIR_WAIT_S + this was lost (api restart)
SEND_TIMEOUT_S = 10.0
POWER_WAIT_S = 4.0
WOL_WAIT_S = 3.0
POLL_STEP_S = 0.5
POLL_S = 4  # how often the TV page asks for the selection
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 15, 300, 30
DEFAULT_SCREEN = "campaign"

SCREENS: dict[str, str] = {
    "flow": "Fiume dei flussi",
    "campaign": "Campagna e portafogli",
    "health": "Salute e avvisi",
}
ADMIN_SCREENS = frozenset({"flow"})  # the «Fiume dei flussi» page is for admins only

NETWORK_NOTE = ("I comandi alla TV partono dal server di SMTrading: la TV deve essere "
                "raggiungibile da lì (stessa rete di casa o una rete privata collegata). "
                "Se non lo è, apri a mano sulla TV il tuo indirizzo personale (anche col "
                "codice QR).")
NOT_PAIRED = "TV non collegata: premi «Collega la TV» e accetta sulla TV"
NOT_SAMSUNG = "a questo indirizzo non risponde una TV Samsung"
NOT_ON = ("la TV non si è accesa: accendila col telecomando e riprova, oppure apri a mano "
          "l'indirizzo (codice QR)")
NOT_HTTPS = ("serve l'indirizzo https del portale: la TV apre le schermate solo in https "
             "(per esempio https://192.168.1.7)")
NO_PORTAL = ("indirizzo del portale per la TV mancante: apri il portale dal suo indirizzo di "
             "rete (non localhost) oppure scrivilo nella sezione")


def allowed_screens(user: AdminUser) -> list[str]:
    return [k for k in SCREENS if user.is_admin or k not in ADMIN_SCREENS]


def screens_dto(user: AdminUser) -> list[dict[str, str]]:
    return [{"key": k, "label": SCREENS[k]} for k in allowed_screens(user)]


def _now() -> datetime:
    return datetime.now(UTC)


# -- validation --------------------------------------------------------------------------


def clean_host(raw: Any) -> str:
    """An IPv4 address of a home network (10/8, 172.16/12, 192.168/16, not .0 or .255:
    `discovery.is_lan_host`): nothing else is ever contacted."""
    try:
        ip = ipaddress.IPv4Address(str(raw or "").strip())
    except ValueError:
        raise _invalid("host", "indirizzo non valido: scrivi l'indirizzo IP della TV, per "
                               "esempio 192.168.1.50") from None
    if not discovery.is_lan_host(ip):
        raise _invalid("host", "solo indirizzi della rete di casa: 10.x, 172.16–31.x o "
                               "192.168.x")
    return str(ip)


def clean_portal_url(raw: Any) -> str | None:
    from urllib.parse import urlsplit

    text = str(raw or "").strip().rstrip("/")
    if not text:
        return None
    parts = urlsplit(text)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path
            or parts.query or parts.fragment or parts.username or len(text) > 200):
        raise _invalid("portal_url", "indirizzo del portale non valido: per esempio "
                                     "https://192.168.1.7")
    return text


def clean_interval(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise _invalid("interval_s", "intervallo: un numero di secondi") from None
    if not INTERVAL_MIN <= n <= INTERVAL_MAX:
        raise _invalid("interval_s", f"intervallo tra {INTERVAL_MIN} e {INTERVAL_MAX} secondi")
    return n


def clean_screen(raw: Any, user: AdminUser) -> str:
    if raw not in SCREENS:
        raise _invalid("screen", "schermata non valida: " + ", ".join(allowed_screens(user)))
    if raw not in allowed_screens(user):
        raise _invalid("screen", "il Fiume dei flussi è solo per gli amministratori")
    return raw


# -- the token ---------------------------------------------------------------------------


def encrypt_token(token: str) -> bytes:
    register_secret(token)
    return encrypt_json({"token": token}, get_settings().master_key, aad=TOKEN_AAD)


def decrypt_token(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        token = decrypt_json(blob, get_settings().master_key, aad=TOKEN_AAD).get("token")
    except (SMTError, ValueError, TypeError, AttributeError):
        return None
    if isinstance(token, str) and token:
        register_secret(token)
        return token
    return None


# -- rows --------------------------------------------------------------------------------


async def own_tv(session: AsyncSession, user_id: uuid.UUID, tv_id: uuid.UUID, *,
                 lock: bool = False) -> TvScreen:
    """The user's own TV; NOT_FOUND for another user's (never told apart from a missing
    one)."""
    q = select(TvScreen).where(TvScreen.id == tv_id, TvScreen.user_id == user_id)
    tv = await session.scalar(q.with_for_update() if lock else q)
    if tv is None:
        raise SMTError(ErrorCode.NOT_FOUND, "TV non trovata")
    return tv


async def user_tvs(session: AsyncSession, user_id: uuid.UUID) -> list[TvScreen]:
    return list(await session.scalars(
        select(TvScreen).where(TvScreen.user_id == user_id)
        .order_by(TvScreen.created_at, TvScreen.id)))


async def get_display(session: AsyncSession, user_id: uuid.UUID, *,
                      lock: bool = False) -> TvDisplay:
    """The user's display row, created (default selection, no link) when missing."""
    q = select(TvDisplay).where(TvDisplay.user_id == user_id)
    d = await session.scalar(q.with_for_update() if lock else q)
    if d is None:
        d = TvDisplay(user_id=user_id, screen=DEFAULT_SCREEN, rotation=False,
                      interval_s=INTERVAL_DEFAULT, version=0, updated_at=_now())
        session.add(d)
        await session.flush()
    return d


def ensure_link(d: TvDisplay) -> None:
    if not d.link_id or tvlink.decrypt_secret(d.link_secret_encrypted) is None:
        rotate_link(d)


def rotate_link(d: TvDisplay) -> None:
    d.link_id, d.link_secret_encrypted, _ = tvlink.new_link()
    d.link_created_at = _now()


def revoke_link(d: TvDisplay) -> None:
    d.link_id = d.link_secret_encrypted = d.link_created_at = None


def url_problem(base: str | None) -> str | None:
    """Why no display URL can be built on `base` (None: it can). The display cookie is
    Secure: the TV page works only over https."""
    if not base:
        return NO_PORTAL
    if not base.lower().startswith("https://"):
        return NOT_HTTPS
    return None


def display_url(base: str | None, d: TvDisplay) -> str | None:
    """`<base>/tv#token=<link>` (a fragment: never sent to a server), or None without an
    https portal address or an active link."""
    token = tvlink.token_for(d.link_id, d.link_secret_encrypted)
    if url_problem(base) is not None or not token:
        return None
    return f"{base}/tv#token={token}"


def record_event(session: AsyncSession, user_id: uuid.UUID, tv_id: uuid.UUID | None,
                 kind: str, d: TvDisplay | None, ok: bool) -> None:
    session.add(TvEvent(user_id=user_id, tv_id=tv_id, kind=kind,
                        screen=d.screen if d is not None else None,
                        rotation=bool(d.rotation) if d is not None else False, ok=ok,
                        created_at=_now()))


# -- DTOs --------------------------------------------------------------------------------


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def pairing_state(tv: TvScreen, now: datetime) -> str:
    if (tv.pairing_state == "waiting" and tv.pairing_started_at is not None
            and now - tv.pairing_started_at > timedelta(seconds=PAIR_WAIT_S + PAIR_STALE_S)):
        return "timeout"
    return tv.pairing_state


def tv_dto(tv: TvScreen, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    state = pairing_state(tv, now)
    message = tv.pairing_message
    if state == "timeout" and tv.pairing_state == "waiting":
        message = "nessuna risposta sulla TV in tempo: riprova e accetta col telecomando"
    return {
        "id": str(tv.id), "name": tv.name, "host": tv.host, "model": tv.model,
        "paired": tv.token_encrypted is not None, "paired_at": _iso(tv.paired_at),
        "pairing": {"state": state, "message": message,
                    "started_at": _iso(tv.pairing_started_at)},
        "last_used_at": _iso(tv.last_used_at), "last_ok": tv.last_ok,
        "last_message": tv.last_message, "revoked_at": _iso(tv.revoked_at),
        "created_at": _iso(tv.created_at),
    }


def display_dto(d: TvDisplay, user: AdminUser) -> dict[str, Any]:
    return {"screen": effective_screen(d, user), "rotation": bool(d.rotation),
            "interval_s": d.interval_s, "version": d.version,
            "updated_at": _iso(d.updated_at), "portal_url": d.portal_url}


def link_dto(d: TvDisplay, base: str | None) -> dict[str, Any]:
    active = bool(d.link_id and d.link_secret_encrypted)
    return {"active": active, "created_at": _iso(d.link_created_at),
            "url": display_url(base, d) if active else None,
            "reason": url_problem(base)}


def effective_screen(d: TvDisplay, user: AdminUser) -> str:
    allowed = allowed_screens(user)
    return d.screen if d.screen in allowed else allowed[0]


# -- pairing (background) ----------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def start_pairing(sessionmaker: async_sessionmaker, tv_id: uuid.UUID, host: str) -> asyncio.Task:
    """Pair in the background (the row is already `waiting`, committed by the caller)."""
    task = asyncio.create_task(_pair_job(sessionmaker, tv_id, host))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


_PAIR_STATES = {"refused": "refused", "timeout": "timeout", "closed": "refused"}


async def _pair_job(sessionmaker: async_sessionmaker, tv_id: uuid.UUID, host: str) -> None:
    token, state, message = None, "ok", "TV collegata"
    try:
        token = await samsung.pair(host, wait_s=PAIR_WAIT_S)
    except samsung.TvError as e:
        state, message = _PAIR_STATES.get(e.kind, "error"), e.message
    except Exception:  # noqa: BLE001
        log.warning("tv: pairing failed unexpectedly", exc_info=False)
        state, message = "error", "collegamento non riuscito"
    try:
        async with sessionmaker() as session:
            tv = await session.scalar(
                select(TvScreen).where(TvScreen.id == tv_id).with_for_update())
            if tv is None or tv.pairing_state != "waiting":
                return
            tv.pairing_state, tv.pairing_message = state, message
            if token:
                tv.token_encrypted = encrypt_token(token)
                tv.paired_at, tv.revoked_at, tv.revoked_by = _now(), None, None
            await session.commit()
    except Exception:
        log.warning("tv: pairing outcome not saved", exc_info=True)


# -- commands ----------------------------------------------------------------------------


def _wol_targets(host: str) -> list[tuple[str, int]]:
    net = ipaddress.IPv4Network(f"{host}/24", strict=False)
    return [(str(net.broadcast_address), samsung.WOL_PORT), ("255.255.255.255", samsung.WOL_PORT)]


async def _wait_power(host: str, seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        await asyncio.sleep(POLL_STEP_S)
        try:
            dev = await samsung.device_info(host, timeout=1.0)
        except samsung.TvError:
            continue
        if dev and dev["power"] == "on":
            return True
    return False


async def _drive_send(host: str, token: str, mac: str | None, url: str) -> tuple[dict, str]:
    """(result, token after the commands): wake the TV if in standby, open the browser."""
    result: dict[str, Any] = {"ok": False, "message": "", "powered_on": False,
                              "app_id": None, "confirmed": False}
    dev = await samsung.device_info(host)
    if dev is None:
        result["message"] = NOT_SAMSUNG
        return result, token
    if dev["power"] != "on":
        async with samsung.open_session(host, token) as s:
            await s.key("KEY_POWER")
            token = s.token or token
        on = await _wait_power(host, POWER_WAIT_S)
        if not on and (mac or dev.get("mac")):
            samsung.wake_on_lan(mac or dev["mac"], _wol_targets(host))
            on = await _wait_power(host, WOL_WAIT_S)
        if not on:
            result["message"] = NOT_ON
            return result, token
        result["powered_on"] = True
    async with samsung.open_session(host, token) as s:
        app_id, confirmed = await s.launch_browser(url)
        token = s.token or token
    result.update(ok=True, app_id=app_id, confirmed=confirmed,
                  message="schermata aperta sulla TV" if confirmed else
                  "comando inviato: se la TV non mostra la pagina, apri l'indirizzo a mano "
                  "(codice QR)")
    return result, token


async def send(host: str, token: str, mac: str | None, url: str) -> tuple[dict, str]:
    """«Manda alla TV», bounded by SEND_TIMEOUT_S; never raises a TvError (the result
    carries the message)."""
    try:
        async with asyncio.timeout(SEND_TIMEOUT_S):
            return await _drive_send(host, token, mac, url)
    except samsung.TvError as e:
        return {"ok": False, "message": e.message, "powered_on": False, "app_id": None,
                "confirmed": False, "reason": e.kind}, token
    except TimeoutError:
        return {"ok": False, "message": samsung.NO_ANSWER, "powered_on": False,
                "app_id": None, "confirmed": False, "reason": "timeout"}, token


async def power_off(host: str, token: str) -> tuple[dict, str]:
    """«Spegni lo schermo»: KEY_POWER only when the TV is on (a TV in standby stays so)."""
    try:
        async with asyncio.timeout(SEND_TIMEOUT_S):
            dev = await samsung.device_info(host)
            if dev is None:
                return {"ok": False, "message": NOT_SAMSUNG}, token
            if dev["power"] != "on":
                return {"ok": True, "message": "la TV è già in standby"}, token
            async with samsung.open_session(host, token) as s:
                await s.key("KEY_POWER")
                token = s.token or token
            return {"ok": True, "message": "TV in standby"}, token
    except samsung.TvError as e:
        return {"ok": False, "message": e.message}, token
    except TimeoutError:
        return {"ok": False, "message": samsung.NO_ANSWER}, token


async def power_state(host: str) -> str:
    """on | standby | unreachable | not_samsung (one short probe)."""
    try:
        dev = await samsung.device_info(host)
    except samsung.TvError:
        return "unreachable"
    return dev["power"] if dev else "not_samsung"


# -- the display link's user -------------------------------------------------------------


async def display_owner(session: AsyncSession, token: str | None
                        ) -> tuple[TvDisplay, AdminUser] | None:
    """The display row and its active user for a valid display token, else None."""
    link_id = tvlink.link_id_of(token)
    if link_id is None:
        return None
    d = await session.scalar(select(TvDisplay).where(TvDisplay.link_id == link_id))
    if d is None or not tvlink.verify(token, d.link_id, d.link_secret_encrypted):
        return None
    user = await session.get(AdminUser, d.user_id)
    if user is None or not user.active:
        return None
    return d, user
