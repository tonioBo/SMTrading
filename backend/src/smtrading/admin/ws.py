"""Admin WebSocket `/ws`: relays Redis pub/sub to the web app.

- Authentication: the `smt_session` cookie, validated like `require_admin` (session in
  Redis, TTL renewed, credentials fingerprint); no session → close 4401. A browser
  `Origin` must match the request `Host` (no CSRF token on WebSockets) → else close 4403;
  a client without `Origin` (not a browser) is allowed.
- Server → client: `{"type":"event","event":{...}}` for every message on `events`,
  `{"type":"quote","quote":{...}}` for the subscribed `ticks:{sym}`, `{"type":"ping"}`
  every `PING_INTERVAL_S` (the session is re-validated at each ping, without renewing its
  TTL: a revoked session closes 4401), `{"type":"subscribed"|"unsubscribed","symbols":[...]}` acknowledgements (sent once
  Redis has confirmed every channel of the request) and
  `{"type":"error","code","message"}` for a malformed client message (or a binary frame).
- Client → server: `{"type":"subscribe_quotes","symbols":[...]}` /
  `{"type":"unsubscribe_quotes","symbols":[...]}`; symbols are canonicalized, at most
  `MAX_SYMBOLS` subscribed per client.

Events of archived portfolios (Task 13: payload `portfolio_id` archived) are not relayed;
`portfolio.archived` / `portfolio.unarchived` themselves are, and update the relay's set
of archived ids (also reloaded from the database at every ping).

One Redis pub/sub connection per client, owned by a single relay task (subscription
changes are queued to it); it is unsubscribed and closed on disconnect. A Redis failure
(or any unexpected error, also during authentication) closes the socket with 1011.
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from smtrading.admin.auth import SESSION_COOKIE, load_admin_session
from smtrading.common.errors import ErrorCode
from smtrading.events import EVENTS_CHANNEL
from smtrading.instruments.service import canonical_symbol
from smtrading.portfolios.models import Portfolio, PortfolioStatus

log = logging.getLogger("smtrading.admin.ws")
router = APIRouter()

WS_PATH = "/ws"
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_INTERNAL_ERROR = 1011
PING_INTERVAL_S = 25.0
MAX_SYMBOLS = 200
MAX_SYMBOL_LEN = 64
TICKS_PREFIX = "ticks:"
_POLL_TIMEOUT_S = 0.1  # bounds the delay before a queued (un)subscription is applied
ARCHIVED_EVENT, UNARCHIVED_EVENT = "portfolio.archived", "portfolio.unarchived"


class _Close(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


_DEFAULT_PORTS = {"http": ":80", "https": ":443"}


def _authority(netloc: str, scheme: str) -> str:
    """host[:port], lowercased, without the scheme's default port."""
    netloc = netloc.lower()
    default = _DEFAULT_PORTS.get(scheme.lower())
    return netloc.removesuffix(default) if default else netloc


def _origin_ok(websocket: WebSocket) -> bool:
    """The browser's Origin (http or https) names the Host the request was sent to; a port
    is compared only when it is not the scheme's default (https://x == Host x:443)."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    host = websocket.headers.get("host", "")
    parts = urlsplit(origin)
    if not host or parts.scheme.lower() not in _DEFAULT_PORTS:
        return False
    return _authority(parts.netloc, parts.scheme) == _authority(host, parts.scheme)


async def _session_valid(websocket: WebSocket, redis: Redis, *, renew: bool) -> bool:
    """The `require_admin` session checks (no CSRF) for the socket's session cookie."""
    async with websocket.app.state.sessionmaker() as db:
        loaded = await load_admin_session(
            redis, db, websocket.cookies.get(SESSION_COOKIE), renew=renew
        )
    return loaded is not None


def _error(message: str) -> dict:
    return {"type": "error", "code": str(ErrorCode.INVALID_ORDER), "message": message}


def _parse_symbols(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            return None
        sym = canonical_symbol(raw)
        if not sym or len(sym) > MAX_SYMBOL_LEN:
            return None
        if sym not in out:
            out.append(sym)
    return out


class _Relay:
    def __init__(self, websocket: WebSocket, redis: Redis) -> None:
        self.ws = websocket
        self.redis = redis
        self.pubsub = redis.pubsub()
        self.symbols: set[str] = set()  # subscribed (or queued for subscription)
        self.commands: asyncio.Queue[tuple[str, list[str]]] = asyncio.Queue()
        # (action, symbols, channels not yet confirmed by Redis), oldest first
        self.pending: deque[tuple[str, list[str], set[str]]] = deque()
        self._send_lock = asyncio.Lock()
        self.archived: set[str] = set()  # ids of the archived portfolios (not relayed)

    async def load_archived(self) -> None:
        async with self.ws.app.state.sessionmaker() as db:
            self.archived = {
                str(pid) for pid in await db.scalars(
                    select(Portfolio.id).where(Portfolio.status == PortfolioStatus.archived)
                )
            }

    def relayed(self, event: Any) -> bool:
        """False for an event of an archived portfolio; tracks (un)archive events."""
        if not isinstance(event, dict):
            return True
        pid = event.get("portfolio_id")
        kind = event.get("type")
        if kind == ARCHIVED_EVENT and isinstance(pid, str):
            self.archived.add(pid)
        elif kind == UNARCHIVED_EVENT and isinstance(pid, str):
            self.archived.discard(pid)
        elif pid in self.archived:
            return False
        return True

    async def send(self, message: dict) -> None:
        async with self._send_lock:
            await self.ws.send_json(message)

    async def receive_loop(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
            text = message.get("text")
            if text is None:
                await self.send(_error("sono ammessi solo messaggi di testo JSON"))
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                await self.send(_error("messaggio non valido"))
                continue
            if not isinstance(msg, dict):
                await self.send(_error("messaggio non valido"))
                continue
            kind = msg.get("type")
            if kind not in ("subscribe_quotes", "unsubscribe_quotes"):
                await self.send(_error("tipo di messaggio sconosciuto"))
                continue
            symbols = _parse_symbols(msg.get("symbols"))
            if symbols is None:
                await self.send(_error("symbols deve essere una lista di simboli"))
                continue
            if kind == "subscribe_quotes":
                new = [s for s in symbols if s not in self.symbols]
                if len(self.symbols) + len(new) > MAX_SYMBOLS:
                    await self.send(_error(f"al massimo {MAX_SYMBOLS} simboli per connessione"))
                    continue
                self.symbols.update(new)
                await self.commands.put(("subscribe", symbols))
            else:
                self.symbols.difference_update(symbols)
                await self.commands.put(("unsubscribe", symbols))

    async def _apply(self, action: str, symbols: list[str]) -> None:
        channels = [TICKS_PREFIX + s for s in symbols]
        self.pending.append((action, symbols, set(channels)))
        if action == "subscribe":
            await self.pubsub.subscribe(*channels)
        else:
            await self.pubsub.unsubscribe(*channels)

    async def _confirmed(self, channel: str) -> None:
        """Redis confirms (un)subscriptions channel by channel, in command order: a batch is
        acknowledged to the client once every one of its channels is confirmed."""
        if not self.pending:
            return
        self.pending[0][2].discard(channel)
        while self.pending and not self.pending[0][2]:
            action, symbols, _ = self.pending.popleft()
            await self.send({"type": f"{action}d", "symbols": symbols})

    async def relay_loop(self) -> None:
        """The only task touching the pub/sub connection."""
        await self.load_archived()
        await self.pubsub.subscribe(EVENTS_CHANNEL)
        while True:
            while not self.commands.empty():
                await self._apply(*self.commands.get_nowait())
            msg = await self.pubsub.get_message(timeout=_POLL_TIMEOUT_S)
            if msg is None:
                continue
            channel = msg["channel"]
            channel = channel.decode() if isinstance(channel, bytes) else channel
            if msg["type"] in ("subscribe", "unsubscribe"):
                await self._confirmed(channel)
                continue
            if msg["type"] != "message":
                continue
            try:
                payload = json.loads(msg["data"])
            except ValueError:
                log.warning("non-JSON message on %s dropped", channel)
                continue
            if channel == EVENTS_CHANNEL:
                if self.relayed(payload):
                    await self.send({"type": "event", "event": payload})
            elif channel.startswith(TICKS_PREFIX) and channel[len(TICKS_PREFIX):] in self.symbols:
                await self.send({"type": "quote", "quote": payload})

    async def ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if not await _session_valid(self.ws, self.redis, renew=False):
                raise _Close(CLOSE_UNAUTHENTICATED)
            await self.load_archived()
            await self.send({"type": "ping"})

    async def close_pubsub(self) -> None:
        with contextlib.suppress(Exception):
            await self.pubsub.unsubscribe()
        with contextlib.suppress(Exception):
            await self.pubsub.aclose()


async def _close(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code)


@router.websocket(WS_PATH)
async def admin_ws(websocket: WebSocket) -> None:
    redis: Redis = websocket.app.state.redis
    if not _origin_ok(websocket):
        await websocket.accept()
        await _close(websocket, CLOSE_FORBIDDEN_ORIGIN)
        return
    try:
        authenticated = await _session_valid(websocket, redis, renew=True)
    except Exception:
        log.exception("websocket authentication failed")
        await websocket.accept()
        await _close(websocket, CLOSE_INTERNAL_ERROR)
        return
    await websocket.accept()
    if not authenticated:
        await _close(websocket, CLOSE_UNAUTHENTICATED)
        return

    relay = _Relay(websocket, redis)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(relay.receive_loop())
            tg.create_task(relay.relay_loop())
            tg.create_task(relay.ping_loop())
    except* WebSocketDisconnect:
        pass
    except* _Close as group:
        await _close(websocket, group.exceptions[0].code)
    except* (RedisError, OSError):
        log.warning("websocket relay stopped: redis error", exc_info=True)
        await _close(websocket, CLOSE_INTERNAL_ERROR)
    except* Exception:
        log.exception("websocket relay failed")
        await _close(websocket, CLOSE_INTERNAL_ERROR)
    finally:
        await relay.close_pubsub()
