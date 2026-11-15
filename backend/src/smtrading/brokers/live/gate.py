"""The live gate: the only HTTP transport a live adapter can use.

`make_live_client` is the only way to build a `LiveClient`, and a `LiveClient` always
sends through a `LiveGateTransport`. Before every request that is not a read, the gate
reads again (in a short session of its own) the link's `live_state`, the linked
portfolio's status, the kill switch and the order being sent:

- GET/HEAD: always passed on, like the POSTs that only read (LIVE_READ_POSTS: eToro
  eligibility, exact URL, never carrying an order);
- DELETE (a cancel): passed on in every live_state of a live link;
- POST/PUT/PATCH: only with the link `armed`, the order's portfolio one of the
  portfolios linked to the link (several since plan 9, Task 11), neither `locked` nor
  `archived`, and the kill switch off; with the kill switch on only an order created by «Chiudi tutte le
  posizioni reali» passes (Ruling R8.2): `live_flatten`, reduce-only, market, still open
  and not acknowledged by the broker, and the request body is that order (same
  instrument and side, no more than its quantity); while a trading halt covers the
  order's portfolio only a reduce-only, live-flatten or liquidation order, or one that strictly
  reduces the position with a matching body, passes (TRADING_HALTED);
- no portfolio linked: closed; an order of a portfolio not linked to the link (or not
  found): closed (OTHER_PORTFOLIO_MESSAGE); a request that is not an order passes only
  when one linked portfolio is neither locked nor archived and none is under a halt;
- `dry_run`: an order POST raises `LiveDryRun` with the JSON payload (no headers), any
  other non-GET raises `LiveGateClosed`.

A closed gate raises before the inner transport is called: no connection is opened and
nothing reaches the broker. The order is identified by the request extension
`smt_order_id` (set by the protocol cores on every order request); an eToro position
close also carries `smt_close` ({symbol, instrument_id, units}), since its body names
neither the symbol nor, for a whole position, the units.
"""

import asyncio
import functools
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
CLOSE_EXTENSION = "smt_close"
READ_METHODS = frozenset({"GET", "HEAD"})
NOT_ARMED_MESSAGE = "denaro reale non attivo su questo collegamento"
KILL_SWITCH_MESSAGE = "kill switch attivo: nessun invio verso il broker reale"
LOCKED_MESSAGE = "portafoglio reale bloccato"
DRY_RUN_MESSAGE = "prova a secco: solo gli ordini vengono intercettati"
OTHER_PORTFOLIO_MESSAGE = "l'ordine non appartiene al portafoglio collegato"
_CLOSED = (PortfolioStatus.locked, PortfolioStatus.archived)


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


_ETORO_CLOSE_KEYS = frozenset({"InstrumentID", "InstrumentId", "UnitsToDeduct"})


@functools.cache
def _etoro_close_url() -> tuple[str, re.Pattern[str]]:
    """(host, path pattern) of the real eToro position close (the adapter's ClassVars)."""
    from smtrading.brokers.live.etoro_live import EtoroLiveAdapter as A

    host = httpx.URL(A.BASE_URL).host
    return host, re.compile(rf"{re.escape(A.EXEC_V1)}/market-close-orders/positions/\d+")


def _etoro_close_units(request: httpx.Request, body: dict, close: object) -> Decimal | None:
    """The units of a well-formed real eToro position close, else None: the close
    endpoint (exact host, no port or userinfo, no query), only the close keys in the
    body, an integer instrument id equal to the `smt_close` extension's, 0 < units and a
    partial body's `UnitsToDeduct` equal to those units."""
    host, path = _etoro_close_url()
    url = request.url
    if (url.scheme != "https" or url.host != host or url.port or url.userinfo
            or url.query or not path.fullmatch(url.path)):
        return None
    if not isinstance(close, dict) or not set(body) <= _ETORO_CLOSE_KEYS:
        return None
    iid = body.get("InstrumentID", body.get("InstrumentId"))
    units = _dec(close.get("units"))
    deduct = body.get("UnitsToDeduct")
    if (not isinstance(iid, int) or isinstance(iid, bool)
            or close.get("instrument_id") != iid
            or units is None or units <= 0
            or (deduct is not None and _dec(deduct) != units)):
        return None
    return units


def _etoro_close_matches(request: httpx.Request, body: dict, close: object, order: Any,
                         prior: Decimal = Decimal(0)) -> bool:
    """eToro position close of this order: well formed (`_etoro_close_units`), the
    extension names the order's symbol, the order is a sell, and with the units of its
    closes already passed (`prior`) no more than the order's open quantity."""
    units = _etoro_close_units(request, body, close)
    if units is None or not isinstance(close, dict):
        return False
    open_qty = order.qty - (order.filled_qty or Decimal(0))
    return (close.get("symbol") == order.symbol and order.side == OrderSide.sell
            and prior + units <= open_qty)


def _body_matches(body: dict, order: Any) -> bool:
    """The request is the order's own closing request: same instrument and side, no more
    than the order's quantity. Alpaca: `symbol`, `side`, `qty`; Trading 212: `ticker`
    (`AAPL_US_EQ`) and a signed `quantity` (negative = sell). An eToro close body is
    matched by `LiveGateTransport._matches` (it needs the request). Anything else: no
    match."""
    if order.qty is None:
        return False
    if "InstrumentID" in body or "InstrumentId" in body:
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


def _read_only_post(request: httpx.Request, method: str) -> bool:
    """A POST to one of LIVE_READ_POSTS (exact URL: no port, no query), carrying no
    order."""
    if method != "POST" or ORDER_EXTENSION in request.extensions or request.url.port:
        return False
    from smtrading.brokers.live import LIVE_READ_POSTS

    url = request.url
    if url.userinfo:
        return False
    return f"{url.scheme}://{url.host}{url.raw_path.decode('ascii', 'replace')}" \
        in LIVE_READ_POSTS


class LiveGateTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, sessionmaker: async_sessionmaker,
                 link_id: uuid.UUID):
        self._inner = inner
        self._sm = sessionmaker
        self._link_id = link_id
        # units of the eToro closes passed per order id: reserved before the request is
        # sent, never released (an unknown outcome counts as closed)
        self._close_units: dict[str, Decimal] = {}
        self._close_lock = asyncio.Lock()  # check and reserve of a close: one at a time

    @property
    def link_id(self) -> uuid.UUID:
        return self._link_id

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        if method not in READ_METHODS and not _read_only_post(request, method):
            await request.aread()
            if CLOSE_EXTENSION in request.extensions:
                async with self._close_lock:
                    await self._check(request, method)
            else:
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
            portfolios = {row.id: row.status for row in (await session.execute(
                select(Portfolio.id, Portfolio.status)
                .where(Portfolio.broker_link_id == self._link_id)
            )).all()}
            if not portfolios:
                raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, NOT_ARMED_MESSAGE)
            order: Any = None
            if order_id is not None:
                order = (await session.execute(
                    select(Order.portfolio_id, Order.live_flatten, Order.reduce_only,
                           Order.type, Order.symbol, Order.side, Order.qty, Order.status,
                           Order.broker_order_id, Order.role, Order.filled_qty)
                    .where(Order.id == order_id)
                )).first()
                if order is None or order.portfolio_id not in portfolios:
                    raise LiveGateClosed(ErrorCode.LIVE_NOT_ARMED, OTHER_PORTFOLIO_MESSAGE)
                if portfolios[order.portfolio_id] in _CLOSED:
                    raise LiveGateClosed(ErrorCode.PORTFOLIO_LOCKED, LOCKED_MESSAGE)
            else:  # not an order: one portfolio able to trade, none under a halt
                if all(status in _CLOSED for status in portfolios.values()):
                    raise LiveGateClosed(ErrorCode.PORTFOLIO_LOCKED, LOCKED_MESSAGE)
            if await is_kill_switch_on(session):
                flatten = (order is not None and order.live_flatten and order.reduce_only
                           and order.type == OrderType.market
                           and order.broker_order_id is None  # not acknowledged yet
                           and order.status in OPEN_STATUSES
                           and self._matches(request, order))
                if not flatten:
                    raise LiveGateClosed(ErrorCode.KILL_SWITCH_ACTIVE, KILL_SWITCH_MESSAGE)
            halted = [order.portfolio_id] if order is not None else sorted(portfolios, key=str)
            for pid in halted:
                halt = await covering_halt(session, pid)
                if halt is not None and not await self._passes_halt(session, request, order):
                    # defense in depth: BrokerSync already refuses openings under a halt
                    raise LiveGateClosed(ErrorCode.TRADING_HALTED, halt_message(halt))
        self._reserve_close(request)  # passed: reserved before the inner transport

    def _matches(self, request: httpx.Request, order: Any) -> bool:
        """The request body is the order's own closing request (`_body_matches`); an
        eToro close also within the order's open quantity with the closes already
        passed for it (cumulative)."""
        body = _payload(request)
        if order.qty is None:
            return False
        if "InstrumentID" in body or "InstrumentId" in body:
            prior = self._close_units.get(str(_order_id(request)), Decimal(0))
            return _etoro_close_matches(request, body,
                                        request.extensions.get(CLOSE_EXTENSION), order, prior)
        return _body_matches(body, order)

    def _reserve_close(self, request: httpx.Request) -> None:
        """An eToro close about to be sent: its units count for its order from now on."""
        order_id = _order_id(request)
        body = _payload(request)
        if order_id is None or not ("InstrumentID" in body or "InstrumentId" in body):
            return
        units = _etoro_close_units(request, body, request.extensions.get(CLOSE_EXTENSION))
        if units is not None:
            key = str(order_id)
            self._close_units[key] = self._close_units.get(key, Decimal(0)) + units

    async def _passes_halt(self, session: Any, request: httpx.Request, order: Any) -> bool:
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
        return reduces_position(order, held) and self._matches(request, order)

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
