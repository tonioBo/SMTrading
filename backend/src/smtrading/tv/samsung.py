"""A small client of the Samsung Tizen TV network API (2016+ models): nothing else is used.

    GET  http://<tv>:8001/api/v2/            the device (name, model, PowerState, wifiMac)
    WSS  wss://<tv>:8002/api/v2/channels/samsung.remote.control?name=<b64>&token=<token>
         the TV's own certificate is self-signed: it is not verified (local network).
         Without a known token the TV asks «Consentire?»; once accepted, the first message
         `ms.channel.connect` carries `data.token` (kept by the caller, encrypted).
         A refusal is `ms.channel.unauthorized`; no answer in time is a timeout.
    key  {"method": "ms.remote.control", "params": {"Cmd": "Click", "DataOfCmd": "KEY_POWER",
         "Option": "false", "TypeOfRemote": "SendRemoteKey"}}
    app  {"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host",
         "data": {"appId": <browser>, "action_type": "NATIVE_LAUNCH", "metaTag": <url>}}}
         tried with each id of BROWSER_APPS: the TV answers `ed.apps.launch` with 200 (opened)
         or an error code (unknown app: the next id); no answer in time is «not confirmed».

Every failure is a `TvError` with a plain Italian message that never holds the token or the
address with its query (the WebSocket URL carries the token): callers show `message` as is.
Every wait is bounded (seconds, below)."""

import asyncio
import base64
import html
import json
import logging
import socket
import ssl
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from smtrading.common.logsetup import register_secret

log = logging.getLogger(__name__)

# the ports and scheme of the TV (module values: the tests point them at a fake TV)
REST_PORT = 8001
# loopback addresses accepted as a TV or a network to search (the tests' fake TV only;
# always False in the application)
ALLOW_LOOPBACK = False
BODY_MAX = 64 * 1024  # the device description is ~2 KB: anything bigger is not a TV
WS_PORT = 8002
WS_SCHEME = "wss"
APP_NAME = "SMTrading"
BROWSER_APPS = ("org.tizen.browser", "com.samsung.tv.browser")
SAMSUNG_TYPE = "Samsung SmartTV"

PROBE_TIMEOUT_S = 2.0
CONNECT_TIMEOUT_S = 4.0  # a command: open and the TV's first message
LAUNCH_ACK_S = 2.5
WOL_PORT = 9

UNREACHABLE = "TV non raggiungibile: controlla che sia accesa (o in standby) e collegata alla rete"
REFUSED = "la TV ha rifiutato il collegamento: premi «Collega la TV» e accetta sulla TV"
NOT_PAIRED = ("la TV chiede di nuovo il permesso: premi «Collega la TV» e accetta col "
              "telecomando")
NO_ANSWER = "la TV non ha risposto in tempo"
NO_TOKEN = "la TV non ha dato il permesso (nessun codice ricevuto)"


class TvError(Exception):
    """A TV failure; `message` is plain Italian, safe to show and to log."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.message, self.kind = message, kind


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the TV's self-signed certificate (local network)
    return ctx


def _device(raw: Any) -> dict[str, Any] | None:
    """The fields SMTrading uses of `GET /api/v2/`, or None when it is not a Samsung TV."""
    if not isinstance(raw, dict):
        return None
    dev = raw.get("device") if isinstance(raw.get("device"), dict) else {}
    if (raw.get("type") or dev.get("type")) != SAMSUNG_TYPE:
        return None
    power = str(dev.get("PowerState") or "on").lower()  # older models: no PowerState = on
    return {
        "name": html.unescape(str(dev.get("name") or raw.get("name") or "TV Samsung")),
        "model": dev.get("modelName") or dev.get("model"),
        "resolution": dev.get("resolution"),
        "mac": dev.get("wifiMac") or None,
        "power": "on" if power == "on" else "standby",
        "token_auth": str(dev.get("TokenAuthSupport") or "").lower() == "true",
    }


async def _read_capped(client: httpx.AsyncClient, url: str, timeout: float) -> Any:
    """The JSON body of a 200 (None otherwise, or beyond BODY_MAX bytes: not a TV)."""
    async with client.stream("GET", url, timeout=timeout) as r:
        if r.status_code != 200:
            return None
        body = b""
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > BODY_MAX:
                return None
    try:
        return json.loads(body)
    except ValueError:
        return None


async def device_info(host: str, *, client: httpx.AsyncClient | None = None,
                      timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any] | None:
    """The TV at `host` (None: something answers, but not a Samsung TV). TvError
    `unreachable` when nothing answers within `timeout`."""
    url = f"http://{host}:{REST_PORT}/api/v2/"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                raw = await _read_capped(c, url, timeout)
        else:
            raw = await _read_capped(client, url, timeout)
    except (httpx.HTTPError, OSError):
        raise TvError(UNREACHABLE, "unreachable") from None
    return _device(raw)


def _ws_url(host: str, token: str | None) -> str:
    name = base64.b64encode(APP_NAME.encode()).decode("ascii")
    url = (f"{WS_SCHEME}://{host}:{WS_PORT}/api/v2/channels/samsung.remote.control"
           f"?name={name}")
    return url + (f"&token={token}" if token else "")


class _Session:
    """One open remote-control connection (`async with open_session(...)`)."""

    def __init__(self, ws, token: str | None) -> None:
        self.ws, self.token = ws, token

    async def send(self, payload: dict) -> None:
        await self.ws.send(json.dumps(payload))

    async def key(self, key: str) -> None:
        await self.send({"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": key, "Option": "false",
            "TypeOfRemote": "SendRemoteKey"}})

    async def next_event(self, event: str, timeout: float) -> dict | None:
        """The next message of `event` within `timeout` (others skipped), else None."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (left := deadline - loop.time()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), left)
            except (TimeoutError, WebSocketException):
                return None
            msg = _parse(raw)
            if msg.get("event") == event:
                return msg
        return None

    async def launch_browser(self, url: str) -> tuple[str, bool]:
        """(app id, confirmed): each browser id in turn until the TV opens one; the first
        one left unanswered stops the round (not confirmed)."""
        for app_id in BROWSER_APPS:
            await self.send({"method": "ms.channel.emit", "params": {
                "event": "ed.apps.launch", "to": "host",
                "data": {"appId": app_id, "action_type": "NATIVE_LAUNCH", "metaTag": url}}})
            reply = await self.next_event("ed.apps.launch", LAUNCH_ACK_S)
            if reply is None:
                return app_id, False
            if str(reply.get("data")) == "200":
                return app_id, True
        raise TvError("la TV non ha aperto il browser: apri l'indirizzo a mano (codice QR)",
                      "launch")


def _parse(raw: Any) -> dict:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}


class open_session:
    """Open the remote-control channel and wait for the TV's first message (`wait_s`):
    `ms.channel.connect` (its `data.token`, if any, on `session.token`), a refusal
    (TvError `refused`) or nothing (TvError `timeout`). With `token` None the TV asks
    the owner (pairing)."""

    def __init__(self, host: str, token: str | None, *, wait_s: float = CONNECT_TIMEOUT_S,
                 pairing: bool = False) -> None:
        self.host, self.token, self.wait_s, self.pairing = host, token, wait_s, pairing
        self._cm = None

    async def __aenter__(self) -> _Session:
        if self.token:
            register_secret(self.token)
        self._cm = connect(
            _ws_url(self.host, self.token), ssl=_ssl_context() if WS_SCHEME == "wss" else None,
            open_timeout=CONNECT_TIMEOUT_S, ping_interval=None, close_timeout=1,
            compression=None, proxy=None, max_size=2**20, user_agent_header=None)
        try:
            ws = await self._cm.__aenter__()
        except (OSError, TimeoutError, InvalidHandshake, WebSocketException):
            self._cm = None
            raise TvError(UNREACHABLE, "unreachable") from None
        try:
            first = await self._first(ws)
        except BaseException:
            await self._close()
            raise
        token = (first.get("data") or {}).get("token") if isinstance(first.get("data"), dict) \
            else None
        token = str(token) if token else self.token
        if token:
            register_secret(token)
        return _Session(ws, token)

    async def _first(self, ws) -> dict:
        try:
            async with asyncio.timeout(self.wait_s):
                while True:
                    msg = _parse(await ws.recv())
                    event = msg.get("event")
                    if event == "ms.channel.connect":
                        return msg
                    if event == "ms.channel.unauthorized":
                        raise TvError(REFUSED, "refused")
                    if event == "ms.channel.timeOut":
                        raise TvError(NO_ANSWER if not self.pairing else
                                      "nessuna risposta sulla TV: riprova e accetta col "
                                      "telecomando", "timeout")
        except TimeoutError:
            if self.pairing:
                raise TvError("nessuna risposta sulla TV in tempo: riprova e accetta col "
                              "telecomando", "timeout") from None
            raise TvError(NOT_PAIRED if self.token else NO_ANSWER, "timeout") from None
        except WebSocketException:
            raise TvError(REFUSED if self.pairing else UNREACHABLE, "closed") from None

    async def _close(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except (OSError, WebSocketException):  # already gone
                pass

    async def __aexit__(self, *exc) -> None:
        await self._close()


async def pair(host: str, *, wait_s: float) -> str:
    """Ask the TV for permission (no token: the TV shows «Consentire?») and wait up to
    `wait_s` for the owner; the token the TV gives (TvError otherwise)."""
    async with open_session(host, None, wait_s=wait_s, pairing=True) as s:
        if not s.token:
            raise TvError(NO_TOKEN, "no_token")
        return s.token


BAD_MAC = "indirizzo MAC della TV non valido: accendila col telecomando"


def magic_packet(mac: str) -> bytes:
    """TvError `mac` for an address that is not 12 hex digits."""
    digits = "".join(c for c in str(mac or "") if c.isalnum())
    try:
        if len(digits) != 12:
            raise ValueError
        return b"\xff" * 6 + bytes.fromhex(digits) * 16
    except ValueError:
        raise TvError(BAD_MAC, "mac") from None


def wake_on_lan(mac: str, targets: list[tuple[str, int]]) -> int:
    """Send the Wake-on-LAN magic packet to each (address, port); the number sent. Best
    effort: from a container on a bridge network a broadcast may never reach the LAN."""
    packet = magic_packet(mac)
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                sock.sendto(packet, target)
                sent += 1
            except OSError:
                log.info("tv: wake-on-lan packet not sent to %s", target[0])
    return sent
