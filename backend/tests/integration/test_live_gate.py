"""The live gate transport: state read again at every non-GET request; a closed gate never
calls the inner transport (an httpx.MockTransport counting the requests)."""

import os
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.live.gate import (
    LiveClient,
    LiveDryRun,
    LiveGateClosed,
    LiveGateTransport,
    make_live_client,
)
from smtrading.brokers.models import BrokerLink
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.settings import set_kill_switch

BASE = "https://broker.invalid"  # never a live host: the inner transport is a mock


class Counting:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"ok": True})
        return httpx.MockTransport(handler)


@pytest.fixture
async def sm(migrated_db):
    """Sessions on one connection inside a rolled-back transaction: the gate's own short
    sessions see the test's rows (savepoints), nothing is ever committed."""
    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


async def _setup(sm, *, state: str | None, locked: bool = False, kill: bool = False,
                 flatten: bool = False, reduce_only: bool = False,
                 type: OrderType = OrderType.market) -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as s:
        s: AsyncSession
        if await s.get(Instrument, "ZZGATE") is None:
            s.add(Instrument(symbol="ZZGATE", name="x", asset_class=AssetClass.us_equity,
                             market="X", currency="USD"))
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind="alpaca_live",
                          live_state=state)
        s.add(link)
        await s.flush()
        p = Portfolio(name=f"P-{uuid.uuid4().hex[:8]}", broker_link_id=link.id,
                      status=PortfolioStatus.locked if locked else PortfolioStatus.active)
        s.add(p)
        await s.flush()
        order = Order(portfolio_id=p.id, symbol="ZZGATE", side=OrderSide.sell, type=type,
                      qty=Decimal(1), reduce_only=reduce_only, live_flatten=flatten,
                      broker_link_id=link.id)
        s.add(order)
        await set_kill_switch(s, kill)
        await s.commit()
        return link.id, order.id


async def _send(client: httpx.AsyncClient, method: str, order_id: uuid.UUID | None):
    ext = {"smt_order_id": str(order_id)} if order_id else None
    body = {"symbol": "ZZGATE", "side": "sell", "qty": "1"} if method == "POST" else None
    return await client.request(method, f"{BASE}/v2/orders", json=body,
                                headers={"APCA-API-SECRET-KEY": "CANARYSECRET-do-not-use"},
                                extensions=ext)


PASS, DRY = "pass", "dry"
NOT_ARMED, KILL, LOCKED = (ErrorCode.LIVE_NOT_ARMED, ErrorCode.KILL_SWITCH_ACTIVE,
                           ErrorCode.PORTFOLIO_LOCKED)


def _expected(state: str, kill: bool, locked: bool, method: str):
    if method == "GET":
        return PASS
    if method == "DELETE":
        return PASS
    if state == "configured":
        return NOT_ARMED
    if state == "dry_run":
        return DRY
    if locked:
        return LOCKED
    if kill:
        return KILL
    return PASS


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("kill", [False, True])
@pytest.mark.parametrize("state", ["configured", "dry_run", "armed"])
async def test_gate_matrix(sm, state, kill, locked, method):
    link_id, order_id = await _setup(sm, state=state, locked=locked, kill=kill)
    inner = Counting()
    expected = _expected(state, kill, locked, method)
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        if expected == PASS:
            r = await _send(client, method, order_id)
            assert r.status_code == 200
            assert len(inner.requests) == 1
        elif expected == DRY:
            with pytest.raises(LiveDryRun):
                await _send(client, method, order_id)
            assert inner.requests == []
        else:
            with pytest.raises(LiveGateClosed) as e:
                await _send(client, method, order_id)
            assert e.value.code == expected
            assert inner.requests == []


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH"])
async def test_paper_or_missing_link_is_closed(sm, method):
    link_id, order_id = await _setup(sm, state=None)
    inner = Counting()
    for target in (link_id, uuid.uuid4()):
        async with make_live_client(sm, target, inner=inner.transport()) as client:
            with pytest.raises(LiveGateClosed) as e:
                await _send(client, method, order_id)
            assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert inner.requests == []


async def test_dry_run_non_order_request_is_closed(sm):
    link_id, _ = await _setup(sm, state="dry_run")
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "PATCH", None)
    assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert inner.requests == []


@pytest.mark.parametrize(("flatten", "reduce_only", "type", "passes"), [
    (True, True, OrderType.market, True),
    (True, False, OrderType.market, False),
    (True, True, OrderType.limit, False),
    (False, True, OrderType.market, False),
])
async def test_flatten_order_bypasses_kill_switch_only_when_reduce_only_market(
        sm, flatten, reduce_only, type, passes):
    link_id, order_id = await _setup(sm, state="armed", kill=True, flatten=flatten,
                                     reduce_only=reduce_only, type=type)
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        if passes:
            assert (await _send(client, "POST", order_id)).status_code == 200
            assert len(inner.requests) == 1
        else:
            with pytest.raises(LiveGateClosed) as e:
                await _send(client, "POST", order_id)
            assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
            assert inner.requests == []
        # a request that is not an order never passes the kill switch
        with pytest.raises(LiveGateClosed):
            await _send(client, "POST", None)


async def test_flatten_order_does_not_pass_a_locked_portfolio(sm):
    link_id, order_id = await _setup(sm, state="armed", kill=True, locked=True, flatten=True,
                                     reduce_only=True)
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", order_id)
    assert e.value.code == ErrorCode.PORTFOLIO_LOCKED
    assert inner.requests == []


async def test_order_of_another_portfolio_is_refused(sm):
    link_id, _ = await _setup(sm, state="armed")
    _, other_order = await _setup(sm, state="armed")
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", other_order)
    assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert inner.requests == []


async def test_state_reread_per_request(sm):
    """Review Focus 2: the kill switch turned on after a first send closes the gate for
    the next one, with the same client."""
    link_id, order_id = await _setup(sm, state="armed")
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        assert (await _send(client, "POST", order_id)).status_code == 200
        async with sm() as s:
            await set_kill_switch(s, True)
            await s.commit()
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", order_id)
        assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
        assert (await _send(client, "DELETE", order_id)).status_code == 200  # cancels pass
        async with sm() as s:
            await set_kill_switch(s, False)
            link = await s.get(BrokerLink, link_id)
            link.live_state = "configured"  # disarmed
            await s.commit()
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", order_id)
        assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert [r.method for r in inner.requests] == ["POST", "DELETE"]


async def test_dry_run_returns_payload_without_headers(sm):
    link_id, order_id = await _setup(sm, state="dry_run")
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveDryRun) as e:
            await _send(client, "POST", order_id)
    assert e.value.payload == {"symbol": "ZZGATE", "side": "sell", "qty": "1"}
    assert e.value.method == "POST"
    assert e.value.path == "/v2/orders"
    assert "CANARY" not in repr(vars(e.value))
    assert inner.requests == []


async def test_live_client_only_from_make_live_client(sm):
    gate = LiveGateTransport(httpx.MockTransport(lambda r: httpx.Response(200)), sm,
                             uuid.uuid4())
    with pytest.raises(TypeError):
        LiveClient(gate, timeout=1.0)
    with pytest.raises(TypeError):
        LiveClient(httpx.MockTransport(lambda r: httpx.Response(200)), timeout=1.0,
                   _maker=object())
    client = make_live_client(sm, uuid.uuid4(), inner=httpx.MockTransport(
        lambda r: httpx.Response(200)))
    assert isinstance(client, LiveClient)
    assert isinstance(client.gate, LiveGateTransport)
    await client.aclose()


@pytest.mark.parametrize("loadable", [True, False])
async def test_default_inner_transport_is_http(sm, monkeypatch, tmp_path, loadable):
    """Deterministic: the result used to depend on the ambient SMT_ environment (an
    exported SMT_LIVE_FAKE_BROKER, or a .env in the working directory). The setting off
    (or settings that cannot be loaded) means the real HTTP transport."""
    from smtrading.common.config import get_settings

    monkeypatch.chdir(tmp_path)  # no .env file
    for name in [k for k in os.environ if k.startswith("SMT_")]:
        monkeypatch.delenv(name)
    if loadable:
        for name, value in (("SMT_DATABASE_URL", "postgresql+asyncpg://u@h/db"),
                            ("SMT_REDIS_URL", "redis://h:6379/0"),
                            ("SMT_MASTER_KEY", "k"), ("SMT_ADMIN_EMAIL", "a@example.com"),
                            ("SMT_ADMIN_PASSWORD", "p")):
            monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        client = make_live_client(sm, uuid.uuid4())
        assert isinstance(client.gate._inner, httpx.AsyncHTTPTransport)
        await client.aclose()
    finally:
        get_settings.cache_clear()


async def _flatten(sm) -> tuple[uuid.UUID, uuid.UUID]:
    return await _setup(sm, state="armed", kill=True, flatten=True, reduce_only=True)


async def _post(client, order_id, body):
    return await client.request("POST", f"{BASE}/v2/orders", json=body,
                                extensions={"smt_order_id": str(order_id)})


@pytest.mark.parametrize(("body", "passes"), [
    ({"symbol": "ZZGATE", "side": "sell", "qty": "1"}, True),
    ({"ticker": "ZZGATE_US_EQ", "quantity": -1}, True),  # Trading 212: sign is the side
    ({"symbol": "OTHER", "side": "sell", "qty": "1"}, False),
    ({"symbol": "ZZGATE", "side": "buy", "qty": "1"}, False),
    ({"ticker": "ZZGATE_US_EQ", "quantity": 1}, False),
    ({"symbol": "ZZGATE", "side": "sell", "qty": "2"}, False),  # more than the order
    ({"side": "sell", "qty": "1"}, False),  # no symbol
    ({"symbol": "ZZGATE", "side": "sell"}, False),  # no quantity
])
async def test_flatten_bypass_checks_the_body_matches_the_order(sm, body, passes):
    link_id, order_id = await _flatten(sm)
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        if passes:
            assert (await _post(client, order_id, body)).status_code == 200
        else:
            with pytest.raises(LiveGateClosed) as e:
                await _post(client, order_id, body)
            assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert len(inner.requests) == (1 if passes else 0)


@pytest.mark.parametrize("change", ["acknowledged", "filled", "canceled"])
async def test_flatten_bypass_only_for_an_unsent_order(sm, change):
    link_id, order_id = await _flatten(sm)
    async with sm() as s:
        o = await s.get(Order, order_id)
        if change == "acknowledged":
            o.broker_order_id = "b-1"
        else:
            o.status = OrderStatus(change)
        await s.commit()
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", order_id)
    assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert inner.requests == []


async def test_two_linked_portfolios_close_the_gate(sm):
    link_id, order_id = await _setup(sm, state="armed")
    async with sm() as s:
        s.add(Portfolio(name=f"P-{uuid.uuid4().hex[:8]}", broker_link_id=link_id))
        await s.commit()
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        with pytest.raises(LiveGateClosed) as e:
            await _send(client, "POST", order_id)
    assert e.value.code == ErrorCode.LIVE_NOT_ARMED
    assert inner.requests == []


async def test_live_client_never_follows_redirects(sm):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/x"})

    async with make_live_client(sm, uuid.uuid4(),
                                inner=httpx.MockTransport(handler)) as client:
        r = await client.get(f"{BASE}/v2/account", follow_redirects=True)
        assert r.status_code == 302
        r = await client.send(client.build_request("GET", f"{BASE}/v2/account"),
                              follow_redirects=True)
        assert r.status_code == 302
        async with client.stream("GET", f"{BASE}/v2/account",
                                 follow_redirects=True) as r:
            assert r.status_code == 302
    assert len(calls) == 3


@pytest.mark.parametrize("kind,passes", [
    ("opening", False), ("reduce_only", True), ("flatten", True), ("liquidation", True),
])
async def test_trading_halt_lets_only_reducing_orders_through(sm, kind, passes):
    """M2: a halt covering the portfolio closes the gate to every write but the
    reduce-only, live-flatten and liquidation orders; reads and cancels still pass."""
    from smtrading.engine.models import LIQUIDATION_ROLE
    from smtrading.risk.models import TradingHalt

    link_id, order_id = await _setup(sm, state="armed", reduce_only=kind == "reduce_only",
                                     flatten=kind == "flatten")
    async with sm() as s:
        order = await s.get(Order, order_id)
        if kind == "liquidation":
            order.role = LIQUIDATION_ROLE
        s.add(TradingHalt(scope="portfolio", portfolio_id=order.portfolio_id,
                          reason="Stop", set_by="admin:admin@example.com"))
        await s.commit()
    inner = Counting()
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        if passes:
            assert (await _send(client, "POST", order_id)).status_code == 200
        else:
            with pytest.raises(LiveGateClosed) as e:
                await _send(client, "POST", order_id)
            assert e.value.code == ErrorCode.TRADING_HALTED
            assert e.value.message == "Aperture sospese dal CEO: Stop"
        with pytest.raises(LiveGateClosed) as e:  # a write that is not an order
            await _send(client, "POST", None)
        assert e.value.code == ErrorCode.TRADING_HALTED
        for method in ("GET", "DELETE"):
            assert (await _send(client, method, order_id)).status_code == 200
    assert len(inner.requests) == (3 if passes else 2)


@pytest.mark.parametrize("held,side,body_qty,passes", [
    ("5", OrderSide.sell, "1", True),    # partial close of a long: strictly reduces
    ("1", OrderSide.sell, "1", True),    # full close
    ("0.5", OrderSide.sell, "1", False),  # larger than the position: would flip short
    ("0", OrderSide.sell, "1", False),   # flat: opens a short
    ("5", OrderSide.buy, "1", False),    # adds to the long
    ("5", OrderSide.sell, "3", False),   # body asks for more than the order
])
async def test_trading_halt_lets_strictly_reducing_orders_through(
        sm, held, side, body_qty, passes):
    """A non-reduce_only order (OCO exit leg, plain close) passes the halt when its side
    closes the position and its quantity is no more than it, and the body is that order."""
    from smtrading.ledger.models import Account, AccountBalance, AccountKind
    from smtrading.risk.models import TradingHalt

    link_id, order_id = await _setup(sm, state="armed")
    async with sm() as s:
        order = await s.get(Order, order_id)
        order.side = side
        acct = Account(portfolio_id=order.portfolio_id, asset="ZZGATE",
                       kind=AccountKind.position)
        s.add(acct)
        await s.flush()
        s.add(AccountBalance(account_id=acct.id, balance=Decimal(held)))
        s.add(TradingHalt(scope="portfolio", portfolio_id=order.portfolio_id,
                          reason="Stop", set_by="admin:admin@example.com"))
        await s.commit()
    inner = Counting()
    body = {"symbol": "ZZGATE", "side": side.value, "qty": body_qty}
    async with make_live_client(sm, link_id, inner=inner.transport()) as client:
        send = client.post(f"{BASE}/v2/orders", json=body,
                           extensions={"smt_order_id": str(order_id)})
        if passes:
            assert (await send).status_code == 200
            assert len(inner.requests) == 1
        else:
            with pytest.raises(LiveGateClosed) as e:
                await send
            assert e.value.code == ErrorCode.TRADING_HALTED
            assert inner.requests == []
