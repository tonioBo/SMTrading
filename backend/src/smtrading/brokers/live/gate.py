"""The live gate: the only HTTP transport a live adapter can use.

`make_live_client` is the only way to build a `LiveClient`, and a `LiveClient` always
sends through a `LiveGateTransport`. Before every request that is not a read, the gate
reads again (in a short session of its own) the link's `live_state`, the linked
portfolio's status, the kill switch and the order being sent:

- GET/HEAD: always passed on;
- DELETE (a cancel): passed on in every live_state of a live link;
- POST/PUT/PATCH: only with the link `armed`, the linked portfolio not `locked` and the
  kill switch off; with the kill switch on only an order created by «Chiudi tutte le
  posizioni reali» passes (Ruling R8.2): `live_flatten`, reduce-only, market, still open
  and not acknowledged by the broker, and the request body is that order (same
  instrument and side, no more than its quantity); while a trading halt covers the
  portfolio only a reduce-only, live-flatten or liquidation order, or one that strictly
  reduces the position with a matching body, passes (TRADING_HALTED);
- exactly one portfolio must be linked to the link (1 live link <-> 1 portfolio);
- `dry_run`: an order POST raises `LiveDryRun` with the JSON payload (no headers), any
  other non-GET raises `LiveGateClosed`.

A closed gate raises before the inner transport is called: no connection is opened and
nothing reaches the broker. The order is identified by the request extension
`smt_order_id` (set by the protocol cores on every order request).
"""

import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers.live.models import LiveState
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode
from smtrading.engine.models import LIQUIDATION_ROLE, OPEN_STATUSES, Order, OrderSide, OrderType
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.halts import covering_halt, halt_message, reduces_position
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


_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _key(symbol: str) -> str:
    return _NON_ALNUM.sub("", symbol.upper())


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _body_matches(body: dict, order: Any) -> bool:
    """The request is the order's own closing request: same instrument and side, no more
    than the order's quantity. Alpaca: `symbol`, `side`, `qty`; Trading 212: `ticker`
    (`AAPL_US_EQ`) and a signed `quantity` (negative = sell). Anything else: no match."""
    if order.qty is None:
        return False
    if "ticker" in body:
        symbol = str(body["ticker"]).split("_")[0]
        qty = _dec(body.get("quantity"))
        side = None if qty is None or qty == 0 else (
            OrderSide.sell if qty < 0 else OrderSide.buy)
    else:
        symbol = str(body.get("symbol") or "")
        qty = _dec(body.get("qty"))
        side = str(body.get("side") or "").lower() or None
    ours = _key(order.symbol.split(".")[0])
    theirs = _key(symbol)
    return (bool(theirs) and bool(ours) and theirs.startswith(ours)
            and qty is not None and side == order.side and 0 < abs(qty) <= order.qty)


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
            portfolios = (await session.execute(
                select(Portfolio.id, Portfolio.status)
                .where(Portfolio.broker_link_id == self._link_id).limit(2)
            )).all()
            if len(portfolios) != 1:  # none, or not a dedicated link (1 link <-> 1 portfolio)
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
            portfolio = portfolios[0]
            if portfolio.status in (PortfolioStatus.locked, PortfolioStatus.archived):
                raise LiveGateClosed(ErrorCode.PORTFOLIO_LOCKED, LOCKED_MESSAGE)
            order: Any = None
            if order_id is not None:
                order = (await session.execute(
                    select(Order.portfolio_id, Order.live_flatten, Order.reduce_only,
                           Order.type, Order.symbol, Order.side, Order.qty, Order.status,
                           Order.broker_order_id, Order.role, Order.filled_qty)
                    .where(Order.id == order_id)
                )).first()
                if order is not None and order.portfolio_id != portfolio.id:
                    raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, OTHER_PORTFOLIO_MESSAGE)
            if await is_kill_switch_on(session):
                flatten = (order is not None and order.live_flatten and order.reduce_only
                           and order.type == OrderType.market
                           and order.broker_order_id is None  # not acknowledged yet
                           and order.status in OPEN_STATUSES
                           and _body_matches(_payload(request), order))
                if not flatten:
                    raise LiveGateClosed(ErrorCode.KILL_SWITCH_ACTIVE, KILL_SWITCH_MESSAGE)
            halt = await covering_halt(session, portfolio.id)
            if halt is not None and not await self._passes_halt(session, request, order):
                # defense in depth: BrokerSync already refuses openings under a halt
                raise LiveGateClosed(ErrorCode.TRADING_HALTED, halt_message(halt))

    @staticmethod
    async def _passes_halt(session: Any, request: httpx.Request, order: Any) -> bool:
        """Under a halt: reduce_only, live_flatten and liquidation orders, or an order that
        strictly reduces the position (closing side, no more than it: OCO exits, plain
        closes) whose request body is that order."""
        if order is None:
            return False
        if order.reduce_only or order.live_flatten or order.role == LIQUIDATION_ROLE:
            return True
        held = await session.scalar(
            select(AccountBalance.balance)
            .join(Account, AccountBalance.account_id == Account.id)
            .where(Account.portfolio_id == order.portfolio_id, Account.asset == order.symbol,
                   Account.kind == AccountKind.position)
        ) or Decimal(0)
        return reduces_position(order, held) and _body_matches(_payload(request), order)

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

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        """Never follows a redirect, whatever the caller asks (request/stream go through
        here too)."""
        kwargs["follow_redirects"] = False
        return await super().send(request, **kwargs)

    @property
    def gate(self) -> LiveGateTransport:
        return self._gate

    @property
    def link_id(self) -> uuid.UUID:
        return self._gate.link_id


def _default_inner() -> httpx.AsyncBaseTransport:
    """The fake only when the setting is explicitly on: settings that cannot be loaded (a
    process or test without the SMT_ environment) mean the setting is off."""
    from pydantic import ValidationError

    from smtrading.common.config import get_settings

    try:
        fake = get_settings().live_fake_broker
    except ValidationError:
        fake = False
    if fake:
        from smtrading.brokers.live.fake import fake_broker

        return fake_broker().transport()
    return httpx.AsyncHTTPTransport()


def make_live_client(sessionmaker: async_sessionmaker, link_id: uuid.UUID, *,
                     inner: httpx.AsyncBaseTransport | None = None,
                     timeout: float = 10.0) -> LiveClient:
    """`inner` defaults to httpx.AsyncHTTPTransport() (respx intercepts it in tests), or to
    the in-memory FakeLiveBroker when the `live_fake_broker` setting is on (e2e only; the
    gate stays in front of it)."""
    gate = LiveGateTransport(inner if inner is not None else _default_inner(),
                             sessionmaker, link_id)
    return LiveClient(gate, timeout=timeout, _maker=_MAKER)
