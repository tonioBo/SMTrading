"""The live gate: the only HTTP transport a live adapter can use.

`make_live_client` is the only way to build a `LiveClient`, and a `LiveClient` always
sends through a `LiveGateTransport`. Before every request that is not a read, the gate
reads again (in a short session of its own) the link's `live_state`, the linked
portfolio's status, the kill switch and the order being sent:

- GET/HEAD: always passed on;
- DELETE (a cancel): passed on in every live_state of a live link;
- POST/PUT/PATCH: only with the link `armed`, the linked portfolio not `locked` and the
  kill switch off; with the kill switch on only an order created by «Chiudi tutte le
  posizioni reali» (`live_flatten`, reduce-only, market) passes (Ruling R8.2);
- `dry_run`: an order POST raises `LiveDryRun` with the JSON payload (no headers), any
  other non-GET raises `LiveGateClosed`.

A closed gate raises before the inner transport is called: no connection is opened and
nothing reaches the broker. The order is identified by the request extension
`smt_order_id` (set by the protocol cores on every order request).
"""

import json
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.models import LiveState
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode
from smtrading.engine.models import Order, OrderType
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.settings import is_kill_switch_on

ORDER_EXTENSION = "smt_order_id"
READ_METHODS = frozenset({"GET", "HEAD"})
NOT_ARMED_MESSAGE = "denaro reale non attivo su questo collegamento"
KILL_SWITCH_MESSAGE = "kill switch attivo: nessun invio verso il broker reale"
LOCKED_MESSAGE = "portafoglio reale bloccato"
DRY_RUN_MESSAGE = "prova a secco: solo gli ordini vengono intercettati"
OTHER_PORTFOLIO_MESSAGE = "l'ordine non appartiene al portafoglio collegato"


class LiveGateClosed(Exception):
    """The request was not sent (definitive: nothing reached the broker)."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LiveDryRun(Exception):
    """Dry run: the order request was intercepted before it was sent."""

    def __init__(self, payload: dict, method: str, path: str):
        super().__init__(f"prova a secco: {method} {path}")
        self.payload = payload
        self.method = method
        self.path = path


def _payload(request: httpx.Request) -> dict:
    """The JSON body of the request (never its headers)."""
    body = request.content
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"body": body.decode("utf-8", "replace")}
    return data if isinstance(data, dict) else {"body": data}


def _order_id(request: httpx.Request) -> uuid.UUID | None:
    value = request.extensions.get(ORDER_EXTENSION)
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


class LiveGateTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, sessionmaker: async_sessionmaker,
                 link_id: uuid.UUID):
        self._inner = inner
        self._sm = sessionmaker
        self._link_id = link_id

    @property
    def link_id(self) -> uuid.UUID:
        return self._link_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        if method not in READ_METHODS:
            await request.aread()
            await self._check(request, method)
        return await self._inner.handle_async_request(request)

    async def _check(self, request: httpx.Request, method: str) -> None:
        """Raises unless the request may reach the broker now (state read again)."""
        order_id = _order_id(request)
        async with self._sm() as session:
            state = await session.scalar(
                select(BrokerLink.live_state).where(BrokerLink.id == self._link_id))
            if state is None or state not in set(LiveState):
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
            if method == "DELETE":
                return  # cancels always pass on a live link
            if state == LiveState.dry_run:
                if order_id is not None:
                    raise LiveDryRun(_payload(request), method, request.url.path)
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, DRY_RUN_MESSAGE)
            if state != LiveState.armed:
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
            portfolio = (await session.execute(
                select(Portfolio.id, Portfolio.status)
                .where(Portfolio.broker_link_id == self._link_id)
            )).first()
            if portfolio is None:
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
            if portfolio.status in (PortfolioStatus.locked, PortfolioStatus.archived):
                raise LiveGateClosed(ErrorCode.PORTFOLIO_LOCKED, LOCKED_MESSAGE)
            order: Any = None
            if order_id is not None:
                order = (await session.execute(
                    select(Order.portfolio_id, Order.live_flatten, Order.reduce_only,
                           Order.type).where(Order.id == order_id)
                )).first()
                if order is not None and order.portfolio_id != portfolio.id:
                    raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, OTHER_PORTFOLIO_MESSAGE)
            if await is_kill_switch_on(session):
                flatten = (order is not None and order.live_flatten and order.reduce_only
                           and order.type == OrderType.market)
                if not flatten:
                    raise LiveGateClosed(ErrorCode.KILL_SWITCH_ACTIVE, KILL_SWITCH_MESSAGE)

    async def aclose(self) -> None:
        await self._inner.aclose()


_MAKER = object()  # only make_live_client holds it


class LiveClient(httpx.AsyncClient):
    """An httpx client whose every request goes through a LiveGateTransport. Built only by
    `make_live_client`: live adapters refuse any other client."""

    def __init__(self, gate: LiveGateTransport, *, timeout: float, _maker: object = None):
        if _maker is not _MAKER or not isinstance(gate, LiveGateTransport):
            raise TypeError("LiveClient is built only by make_live_client")
        # no mounts, no proxies from the environment: the gate is the only transport
        super().__init__(transport=gate, timeout=timeout, trust_env=False,
                         follow_redirects=False)
        self._gate = gate

    @property
    def gate(self) -> LiveGateTransport:
        return self._gate

    @property
    def link_id(self) -> uuid.UUID:
        return self._gate.link_id


def make_live_client(sessionmaker: async_sessionmaker, link_id: uuid.UUID, *,
                     inner: httpx.AsyncBaseTransport | None = None,
                     timeout: float = 10.0) -> LiveClient:
    """`inner` defaults to httpx.AsyncHTTPTransport() (respx intercepts it in tests)."""
    gate = LiveGateTransport(inner or httpx.AsyncHTTPTransport(), sessionmaker, link_id)
    return LiveClient(gate, timeout=timeout, _maker=_MAKER)
