"""eToro real money (plan 9, Task 16): EtoroLiveAdapter behind the plan-8 live gate.

Never reaches the network: the broker is the in-memory FakeLiveBroker behind the gate
(its `real` environment), the conftest socket guard blocks the eToro host. Only canary
keys. No real path is written here: they come from EtoroLiveAdapter's ClassVars (the
repository scan of tests/unit/test_live_hosts_guard.py checks it).
"""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pyotp
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.admin.models import AdminUser
from smtrading.brokers.etoro_core import ETORO_URL, account_digest
from smtrading.brokers.etoro_demo import EtoroDemoAdapter
from smtrading.brokers.live import CASH_ONLY_KINDS, LIVE_HOSTS, LIVE_KINDS, LIVE_PATHS, hostname
from smtrading.brokers.live.account import (
    LIVE_FIELDS,
    account_fingerprint,
    encrypt_live_config,
    key_masked,
)
from smtrading.brokers.live.etoro_live import EtoroLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import LiveDryRun, LiveGateClosed, make_live_client
from smtrading.brokers.live.models import LiveAudit
from smtrading.brokers.live.reconcile import reconcile_live
from smtrading.brokers.live.service import (
    ETORO_CASH_ACCOUNT_MESSAGE,
    Actor,
    arm_live_link,
    disarm_live_link,
    link_live_portfolio,
    save_live_keys,
    set_dry_run,
    test_live_link,
)
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.registry import adapter_class
from smtrading.brokers.sync import BrokerSync
from smtrading.common import logsetup
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order
from smtrading.events import dispatch_post_commit
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio
from smtrading.risk.models import RiskProfile, TradingHalt
from smtrading.risk.settings import set_kill_switch
from smtrading.seed import run_seed
from smtrading.telegram.config import TelegramConfig, save_config
from smtrading.telegram.models import TelegramOutbox

D = Decimal
A = EtoroLiveAdapter
API_KEY = "CANARYETOROAPI0001"
USER_KEY = "CANARYETOROUSER-real-do-not-use"
CFG = {"api_key": API_KEY, "user_key": USER_KEY}
CANARIES = (API_KEY, USER_KEY)
AAPL_ID = 1001  # FakeLiveBroker's eToro instrument ids
OPEN = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
WHO = Actor("admin@example.com", "10.0.0.1", "pytest")
CLOSE_PATH = f"{A.EXEC_V1}/market-close-orders/positions"


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registered_secrets():
    saved = set(logsetup._registered)
    logsetup._registered.clear()
    yield
    logsetup._registered.clear()
    logsetup._registered.update(saved)


@pytest.fixture
async def sm(migrated_db):
    """Sessions inside one rolled-back transaction (the gate's short sessions see the
    test's rows; nothing is committed for real)."""
    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
        async with maker() as s:
            await run_seed(s)
            await s.commit()
        try:
            yield maker
        finally:
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def maker(fresh_db, monkeypatch):
    """A committed database (BrokerSync and the reconciliation open their own sessions)."""
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    engine = make_engine(fresh_db)
    m = async_sessionmaker(engine, expire_on_commit=False)
    async with m() as s:
        await run_seed(s)
        await s.commit()
    yield m
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    yield r
    await r.aclose()


class Fake(FakeLiveBroker):
    """FakeLiveBroker recording every request reaching it (method, path, headers), able to
    answer 401 echoing the keys."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []
        self.echo_keys = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method.upper(), request.url.path))
        if self.echo_keys:
            return httpx.Response(401, json={"errorMessage": "bad keys "
                                             + request.headers.get("x-api-key", "") + " "
                                             + request.headers.get("x-user-key", "")})
        return super()._handle(request)

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.seen if m not in ("GET", "HEAD")]


@pytest.fixture
def fake():
    return Fake()


def _hold(fake: FakeLiveBroker, units: str = "2", *, leverage: int = 1, pid: int = 901,
          iid: int = AAPL_ID) -> int:
    """A position of the fake's real account (opened before, outside SMTrading)."""
    fake.etoro["real"].positions[pid] = {
        "positionID": pid, "instrumentID": iid, "isBuy": True, "units": D(units),
        "openRate": D(100), "amount": D(units) * 100, "leverage": leverage, "mirrorID": 0,
        "settlementTypeID": 1, "totalFees": D(0), "openDateTime": "2026-11-10T14:00:00Z"}
    return pid


async def _instrument(sm, symbol: str = "AAPL") -> Instrument:
    async with sm() as s:
        return await s.get(Instrument, symbol)


async def _gate_link(sm, *, state: str, kill: bool = False, side=OrderSide.sell, qty="2",
                     flatten: bool = False, held: str | None = None
                     ) -> tuple[uuid.UUID, Order]:
    """An etoro_live link (canary keys) with one portfolio and one order of AAPL."""
    async with sm() as s:
        link = BrokerLink(name=f"E-{uuid.uuid4().hex[:8]}", kind="etoro_live",
                          live_state=state, config_encrypted=encrypt_live_config(CFG))
        s.add(link)
        await s.flush()
        p = Portfolio(name=f"P-{uuid.uuid4().hex[:8]}", broker_link_id=link.id,
                      status=PortfolioStatus.active)
        s.add(p)
        await s.flush()
        order = Order(portfolio_id=p.id, symbol="AAPL", side=side, type=OrderType.market,
                      qty=D(qty), reduce_only=flatten, live_flatten=flatten,
                      broker_link_id=link.id)
        s.add(order)
        if held is not None:
            acct = Account(portfolio_id=p.id, asset="AAPL", kind=AccountKind.position)
            s.add(acct)
            await s.flush()
            s.add(AccountBalance(account_id=acct.id, balance=D(held)))
        await set_kill_switch(s, kill)
        await s.commit()
        return link.id, order


def _adapter(sm, link_id, fake, cls=EtoroLiveAdapter):
    return cls(dict(CFG), make_live_client(sm, link_id, inner=fake.transport()))


# -- construction and path guards ----------------------------------------------------------

def test_live_kind_registered_and_cash_only():
    assert "etoro_live" in LIVE_KINDS and "etoro_live" in CASH_ONLY_KINDS
    assert adapter_class("etoro_live") is EtoroLiveAdapter
    assert (A.kind, A.live, A.idempotent_place, A.BASE_URL, A.ENV) == (
        "etoro_live", True, False, ETORO_URL, "real")
    assert LIVE_FIELDS["etoro_live"] == ("user_key", "api_key", frozenset())
    assert hostname(ETORO_URL) not in LIVE_HOSTS  # shared with the demo: the guard is the path


async def test_live_requires_gate_client():
    for http in (httpx.AsyncClient(), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200)))):
        with pytest.raises(SMTError) as e:
            EtoroLiveAdapter(dict(CFG), http)
        assert e.value.code == ErrorCode.FORBIDDEN
        assert e.value.message == "gli adattatori live richiedono il cancello"
        await http.aclose()
    client = make_live_client(None, uuid.uuid4(), inner=Fake().transport())
    EtoroLiveAdapter(dict(CFG), client)  # the gate's own client: accepted
    await client.aclose()


@pytest.mark.parametrize("base", ["https://evil.example", "http://public-api.etoro.com",
                                  ETORO_URL + "/api"])
async def test_live_rejects_other_base_url(base):
    client = make_live_client(None, uuid.uuid4(), inner=Fake().transport())
    with pytest.raises(SMTError) as e:
        EtoroLiveAdapter({**CFG, "base_url": base}, client)
    assert e.value.code == ErrorCode.FORBIDDEN
    EtoroLiveAdapter({**CFG, "base_url": ETORO_URL + "/"}, client)  # the constant itself
    await client.aclose()


def _demo_paths() -> list[str]:
    d = EtoroDemoAdapter
    return [f"{d.EXEC_V2}/orders", f"{d.EXEC_V2}/orders/7", f"{d.INFO_V2}/eligibility",
            f"{d.INFO_V2}/orders:lookup?orderId=7", f"{d.INFO_V1}/pnl",
            f"{d.INFO_V1}/close-orders/7", f"{d.EXEC_V1}/market-close-orders/positions/7",
            f"{A.EXEC_V2}/x/../demo/orders", f"{A.EXEC_V2}/%64emo/orders"]


def _real_paths() -> list[str]:
    return [f"{A.EXEC_V2}/orders", f"{A.EXEC_V2}/orders/7", f"{A.INFO_V2}/eligibility",
            f"{A.INFO_V2}/orders:lookup?orderId=7", f"{A.INFO_V1}/pnl",
            f"{A.INFO_V1}/close-orders/7", f"{CLOSE_PATH}/7"]


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_live_adapter_cannot_call_demo_path(sm, fake, method):
    link_id, _ = await _gate_link(sm, state="armed")
    adapter = _adapter(sm, link_id, fake)
    for path in _demo_paths():
        with pytest.raises(SMTError) as e:
            await adapter._request(method, path, body={} if method == "POST" else None)
        assert e.value.code == ErrorCode.FORBIDDEN, path
    assert fake.seen == []
    await adapter.http.aclose()


def test_live_paths_never_contain_demo():
    assert {A.EXEC_V1, A.EXEC_V2, A.INFO_V1, A.INFO_V2} == LIVE_PATHS
    for path in LIVE_PATHS | set(A.ALLOWED_PREFIXES) | A.READ_ONLY_POSTS:
        assert "demo" not in path.lower(), path
    demo = {EtoroDemoAdapter.EXEC_V1, EtoroDemoAdapter.EXEC_V2, EtoroDemoAdapter.INFO_V1,
            EtoroDemoAdapter.INFO_V2}
    assert not demo & LIVE_PATHS
    for prefix in demo:
        assert "/demo" in prefix


@pytest.mark.parametrize("state", ["armed", "dry_run"])
async def test_demo_adapter_never_reaches_real_paths_even_on_live_client(sm, fake, state):
    """The demo adapter handed a LiveClient of a live link still cannot reach a real path:
    its guard refuses before the gate and the network."""
    link_id, _ = await _gate_link(sm, state=state)
    adapter = _adapter(sm, link_id, fake, cls=EtoroDemoAdapter)
    for method in ("GET", "POST", "DELETE"):
        for path in _real_paths():
            with pytest.raises(SMTError) as e:
                await adapter._request(method, path, body={} if method == "POST" else None)
            assert e.value.code == ErrorCode.FORBIDDEN, path
    assert fake.seen == []
    await adapter.http.aclose()


# -- dry run, kill switch, halt ------------------------------------------------------------

async def test_dry_run_intercepts_open_and_close(sm, fake):
    """In dry run the first order POST (an opening or a close) raises LiveDryRun with its
    payload; the reads (search, the eligibility POST, pnl) pass, no write reaches eToro."""
    inst = await _instrument(sm)
    link_id, sell = await _gate_link(sm, state="dry_run", qty="2")
    _hold(fake, "2")
    async with sm() as s:
        buy = Order(portfolio_id=sell.portfolio_id, symbol="AAPL", side=OrderSide.buy,
                    type=OrderType.market, qty=D(1), broker_link_id=link_id)
        s.add(buy)
        await s.commit()
    adapter = _adapter(sm, link_id, fake)
    with pytest.raises(LiveDryRun) as e:
        await adapter.place_order(buy, inst)
    assert (e.value.method, e.value.path) == ("POST", f"{A.EXEC_V2}/orders")
    assert e.value.payload["leverage"] == 1 and e.value.payload["transaction"] == "buy"
    assert e.value.payload["settlementType"] == "real"
    assert e.value.payload["instrumentId"] == AAPL_ID
    with pytest.raises(LiveDryRun) as e:
        await adapter.place_order(sell, inst)
    assert e.value.path == f"{CLOSE_PATH}/901"
    assert e.value.payload == {"InstrumentID": AAPL_ID, "UnitsToDeduct": None}
    assert fake.writes() == [("POST", f"{A.INFO_V2}/eligibility")]  # a read, by eToro's API
    assert fake.etoro["real"].positions[901]["units"] == D(2)  # nothing closed
    assert all("/demo/" not in p for _, p in fake.seen)
    await adapter.http.aclose()


async def test_eligibility_post_is_a_read_only_on_its_exact_url(sm, fake):
    """The gate passes the eligibility POST like a GET only on its exact URL and without
    an order; any other POST of a configured link stays closed."""
    link_id, order = await _gate_link(sm, state="configured")
    async with make_live_client(sm, link_id, inner=fake.transport()) as client:
        r = await client.post(f"{ETORO_URL}{A.INFO_V2}/eligibility", json={"instrumentIds": []},
                              headers={"x-api-key": "k", "x-user-key": "u"})
        assert r.status_code == 200
        for url, ext in [
            (f"{ETORO_URL}{A.INFO_V2}/eligibility?x=1", None),
            (f"{ETORO_URL}{A.INFO_V2}/eligibility", {"smt_order_id": str(order.id)}),
            (f"https://other.invalid{A.INFO_V2}/eligibility", None),
            (f"{ETORO_URL}:8443{A.INFO_V2}/eligibility", None),
            (f"{ETORO_URL}{A.EXEC_V2}/orders", None),
        ]:
            with pytest.raises(LiveGateClosed) as e:
                await client.post(url, json={}, extensions=ext)
            assert e.value.code == ErrorCode.LIVE_NOT_ARMED, url
    assert fake.writes() == [("POST", f"{A.INFO_V2}/eligibility")]


def _close_ext(**kw) -> dict:
    return {"symbol": "AAPL", "instrument_id": AAPL_ID, "units": "2", **kw}


@pytest.mark.parametrize(("body", "ext", "passes"), [
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, _close_ext(), True),
    ({"InstrumentId": AAPL_ID, "UnitsToDeduct": None}, _close_ext(), True),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": "1.5"}, _close_ext(units="1.5"), True),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, None, False),  # no extension
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, _close_ext(instrument_id=1002), False),
    ({"InstrumentID": 1002, "UnitsToDeduct": None}, _close_ext(), False),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, _close_ext(symbol="MSFT"), False),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, _close_ext(units="3"), False),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": None}, _close_ext(units="0"), False),
    ({"InstrumentID": AAPL_ID, "UnitsToDeduct": "2"}, _close_ext(units="1"), False),
    ({"InstrumentID": str(AAPL_ID), "UnitsToDeduct": None}, _close_ext(), False),
    ({"instrumentId": AAPL_ID, "transaction": "buy", "leverage": 1}, _close_ext(), False),
])
async def test_kill_switch_flatten_close_passes_only_with_matching_extension(
        sm, fake, body, ext, passes):
    """Kill switch on: only a «Chiudi tutte le posizioni reali» close whose `smt_close`
    names the body's instrument, the order's symbol and 0 < units <= the order's qty."""
    link_id, order = await _gate_link(sm, state="armed", kill=True, flatten=True)
    extensions = {"smt_order_id": str(order.id)}
    if ext is not None:
        extensions["smt_close"] = ext
    async with make_live_client(sm, link_id, inner=fake.transport()) as client:
        send = client.post(f"{ETORO_URL}{CLOSE_PATH}/901", json=body,
                           headers={"x-api-key": "k", "x-user-key": "u"}, extensions=extensions)
        if passes:
            await send  # reached the fake (404: no such position there)
        else:
            with pytest.raises(LiveGateClosed) as e:
                await send
            assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert len(fake.seen) == (1 if passes else 0)


async def test_kill_switch_flatten_through_the_adapter(sm, fake):
    """The adapter's closes of a flatten order pass the kill switch (whole positions,
    FIFO); a buy does not."""
    inst = await _instrument(sm)
    link_id, flatten = await _gate_link(sm, state="armed", kill=True, flatten=True, qty="3")
    _hold(fake, "1", pid=901)
    _hold(fake, "2", pid=902)
    adapter = _adapter(sm, link_id, fake)
    bo = await adapter.place_order(flatten, inst)
    assert bo.status == "accepted" and bo.broker_order_id.startswith("close:3:")
    assert fake.etoro["real"].positions == {}
    async with sm() as s:
        buy = Order(portfolio_id=flatten.portfolio_id, symbol="AAPL", side=OrderSide.buy,
                    type=OrderType.market, qty=D(1), broker_link_id=link_id)
        s.add(buy)
        await s.commit()
    with pytest.raises(LiveGateClosed) as e:
        await adapter.place_order(buy, inst)
    assert e.value.code == ErrorCode.KILL_SWITCH_ACTIVE
    assert [p for m, p in fake.writes() if p.endswith("/orders")] == []
    await adapter.http.aclose()


@pytest.mark.parametrize(("ext", "passes"), [(_close_ext(units="1"), True), (None, False)])
async def test_trading_halt_lets_a_matching_close_through(sm, fake, ext, passes):
    """Under a halt a plain sell that strictly reduces the position passes only when its
    close request is that order (the `smt_close` extension)."""
    link_id, order = await _gate_link(sm, state="armed", qty="1", held="5")
    async with sm() as s:
        s.add(TradingHalt(scope="portfolio", portfolio_id=order.portfolio_id, reason="Stop",
                          set_by="admin:admin@example.com"))
        await s.commit()
    extensions = {"smt_order_id": str(order.id)}
    if ext is not None:
        extensions["smt_close"] = ext
    async with make_live_client(sm, link_id, inner=fake.transport()) as client:
        send = client.post(f"{ETORO_URL}{CLOSE_PATH}/901",
                           json={"InstrumentID": AAPL_ID, "UnitsToDeduct": "1"},
                           headers={"x-api-key": "k", "x-user-key": "u"}, extensions=extensions)
        if passes:
            await send
        else:
            with pytest.raises(LiveGateClosed) as e:
                await send
            assert e.value.code == ErrorCode.TRADING_HALTED
    assert len(fake.seen) == (1 if passes else 0)


# -- BrokerSync and reconciliation ---------------------------------------------------------

def _qs() -> FakeQuoteSource:
    q = FakeQuoteSource()
    q.set_fx("EUR", "USD", D("1.25"))
    q.set_fx("USD", "EUR", D("0.8"))
    q.set_quote("AAPL", D("99.90"), D("100.00"), D("99.95"))
    return q


class _Clock:
    def __call__(self) -> datetime:
        return OPEN


async def _committed_link(maker, *, state="armed", ccy="USD", cash="1000"
                          ) -> tuple[uuid.UUID, uuid.UUID]:
    async with maker() as s:
        link = BrokerLink(
            name=f"E-{uuid.uuid4().hex[:8]}", kind="etoro_live", live_state=state,
            config_encrypted=encrypt_live_config(CFG),
            account_fingerprint=account_fingerprint("etoro_live", account_digest(USER_KEY)),
            last_account_snapshot={"currency": "USD", "cash_available": "1000"},
            last_snapshot_at=OPEN)
        s.add(link)
        await s.flush()
        p = await create_portfolio(s, name=f"Reale {uuid.uuid4().hex[:6]}",
                                   initial_cash=D(cash), base_currency=ccy, leverage=D(1))
        p.broker_link_id = link.id
        await s.commit()
        return p.id, link.id


async def test_armed_buy_is_unleveraged_and_audited(maker, redis, fake, monkeypatch):
    """Agent buy -> BrokerSync -> the gate (armed) -> one unleveraged real opening, then
    the fill from the order lookup; the payload is in the live audit, never a key."""
    monkeypatch.setattr("smtrading.brokers.sync._default_inner", fake.transport)
    qs = _qs()
    pid, _ = await _committed_link(maker)
    async with maker() as s:
        order = await place_order(s, qs, PlaceOrderRequest(
            portfolio_id=pid, symbol="AAPL", side=OrderSide.buy, type=OrderType.market,
            qty=D(2), agent_id="agent-1"), now=OPEN)
        await s.commit()
        await dispatch_post_commit(s, redis)
        oid = order.id
    sync = BrokerSync(maker, redis, qs, None, clock=_Clock())
    try:
        for _ in range(3):
            await sync.tick()
            async with maker() as s:
                if (await s.get(Order, oid)).status == OrderStatus.filled:
                    break
    finally:
        await sync.aclose()
    async with maker() as s:
        o = await s.get(Order, oid)
        audit = list(await s.scalars(select(LiveAudit).where(LiveAudit.order_id == oid)))
    assert o.status == OrderStatus.filled and o.avg_fill_price == D(100), o.reject_message
    opens = [(m, p) for m, p in fake.writes() if p == f"{A.EXEC_V2}/orders"]
    assert opens == [("POST", f"{A.EXEC_V2}/orders")]
    assert all("/demo/" not in p for _, p in fake.seen)
    sent = next(a for a in audit if a.action == "order_sent")
    payload = sent.details["payload"]
    assert (payload["action"], payload["transaction"], payload["leverage"],
            payload["settlementType"], payload["orderType"]) == ("open", "buy", 1, "real", "mkt")
    assert "sellShort" not in payload and D(str(payload["units"])) == D(2)
    pos = next(iter(fake.etoro["real"].positions.values()))
    assert (pos["leverage"], pos["isBuy"], pos["units"]) == (1, True, D(2))
    assert not any(c in str(a.details) for a in audit for c in CANARIES)


async def test_nonconforming_position_trips_margin_account(maker, fake):
    """R8.10 for eToro: a leveraged position on the account (opened from the eToro app)
    makes the snapshot `leveraged`: the reconciliation puts the portfolio in reduce-only
    (`margin_account`) with a critical alert."""
    from smtrading.activity.models import Activity

    pid, link_id = await _committed_link(maker)
    _hold(fake, "1", leverage=2)
    await reconcile_live(maker, link_id,
                         lambda lid: make_live_client(maker, lid, inner=fake.transport()),
                         qs=_qs())
    async with maker() as s:
        p = await s.get(Portfolio, pid)
        link = await s.get(BrokerLink, link_id)
        alerts = list(await s.scalars(select(Activity).where(
            Activity.type == "live.reduce_only", Activity.portfolio_id == pid)))
    assert (p.status, p.reduce_only_reason) == (PortfolioStatus.reduce_only,
                                                "live_margin_account")
    assert link.last_account_snapshot["account_type"] == "leveraged"
    assert [a.data["cause"] for a in alerts] == ["margin_account"]
    assert alerts[0].level == "critical"
    assert fake.writes() == []


# -- activation flow -----------------------------------------------------------------------

class Totp:
    def __init__(self, secret: str) -> None:
        self.totp = pyotp.TOTP(secret)
        self.used: set[str] = set()

    def code(self) -> str:
        now = time.time()
        for t in (now + 30, now, now - 30):
            c = self.totp.at(t)
            if c not in self.used:
                self.used.add(c)
                return c
        raise AssertionError("no unused TOTP code left in the window")


async def _admin(sm) -> tuple[AdminUser, Totp]:
    secret = pyotp.random_base32()
    async with sm() as s:
        user = AdminUser(
            email=f"a-{uuid.uuid4().hex[:6]}@example.com", password_hash="x",
            totp_secret_encrypted=encrypt_json({"secret": secret}, get_settings().master_key),
            totp_enabled=True)
        s.add(user)
        await s.commit()
        return user, Totp(secret)


async def _telegram(sm) -> None:
    async with sm() as s:
        await save_config(s, TelegramConfig(bot_token="123456789:AAH" + "x" * 32,
                                            chat_id="-100123", enabled=True))
        s.add(TelegramOutbox(source="system", category="test", level="info", text="prova",
                             status="sent", sent_at=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()


async def _usd_portfolio(sm) -> uuid.UUID:
    async with sm() as s:
        profile = RiskProfile(name=f"Live-{uuid.uuid4().hex[:6]}",
                              max_order_notional_eur=D(500), max_daily_loss_pct=D(5),
                              short_allowed=False)
        s.add(profile)
        await s.flush()
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:6]}", initial_cash=D(500),
                                   base_currency="USD", leverage=D(1),
                                   risk_profile_id=profile.id)
        await s.commit()
        return p.id


async def _actions(sm, link_id) -> list[str]:
    async with sm() as s:
        return list(await s.scalars(select(LiveAudit.action).where(
            LiveAudit.link_id == link_id).order_by(LiveAudit.id)))


async def _activation_flow(sm, redis, fake) -> tuple[uuid.UUID, dict]:
    """keys -> read-only test -> dry run -> link (TOTP) -> arm (TOTP) -> disarm."""
    admin, totp = await _admin(sm)
    await _telegram(sm)
    async with sm() as s:
        link = await save_live_keys(s, name="eToro reale", kind="etoro_live",
                                    config=dict(CFG), actor=WHO)
        await s.commit()
        link_id = link.id
    async with make_live_client(sm, link_id, inner=fake.transport()) as http:
        result = await test_live_link(sm, link_id, actor=WHO, http=http)
    async with sm() as s:
        await set_dry_run(s, link_id, True, actor=WHO)
        await s.commit()
    pid = await _usd_portfolio(sm)
    async with sm() as s:
        await link_live_portfolio(s, redis, pid, link_id,
                                  admin=await s.get(AdminUser, admin.id), totp=totp.code(),
                                  actor=WHO, qs=_qs())
        await s.commit()
    async with sm() as s:
        await arm_live_link(s, redis, link_id, admin=await s.get(AdminUser, admin.id),
                            totp=totp.code(), confirm_name="eToro reale", accept_risks=True,
                            dedicated_account=True, limits_set=True, actor=WHO, qs=_qs())
        await s.commit()
    async with sm() as s:
        assert (await s.get(BrokerLink, link_id)).live_state == "armed"
        await disarm_live_link(s, link_id, actor=WHO, reason="fine prova")
        await s.commit()
    return link_id, result


async def test_activation_flow_etoro_with_canary_keys(sm, redis, fake):
    link_id, result = await _activation_flow(sm, redis, fake)
    assert result["empty"] is True and result["currency"] == "USD"
    assert result["account_type"] == "cash"
    async with sm() as s:
        link = await s.get(BrokerLink, link_id)
    assert link.live_state == "configured" and link.disarmed_reason
    assert link.account_fingerprint == account_fingerprint(
        "etoro_live", account_digest(USER_KEY))
    assert key_masked("etoro_live", CFG) == "••••" + USER_KEY[-4:]
    actions = await _actions(sm, link_id)
    assert actions[:2] == ["keys_saved", "test_ok"]
    assert {"dry_run_on", "portfolio_linked", "armed", "disarmed"} <= set(actions)
    assert fake.writes() == []  # the whole flow only reads
    assert all("/demo/" not in p for _, p in fake.seen)
    assert ("GET", f"{A.INFO_V1}/pnl") in fake.seen


@pytest.mark.parametrize(("config", "field"), [
    ({"api_key": API_KEY}, "user_key"),
    ({"user_key": USER_KEY}, "api_key"),
    ({**CFG, "api_secret": "x"}, "api_secret"),
    ({"api_key": API_KEY, "user_key": "••••" + USER_KEY[-4:]}, "user_key"),
])
async def test_etoro_keys_validation(sm, config, field):
    async with sm() as s:
        with pytest.raises(SMTError) as e:
            await save_live_keys(s, name="X", kind="etoro_live", config=config, actor=WHO)
    assert e.value.code == ErrorCode.INVALID_ORDER and e.value.details["field"] == field


async def test_etoro_leveraged_account_fails_the_test_and_dry_run(sm, fake):
    async with sm() as s:
        link = await save_live_keys(s, name="eToro reale", kind="etoro_live",
                                    config=dict(CFG), actor=WHO)
        await s.commit()
        link_id = link.id
    _hold(fake, "1", leverage=2)
    async with make_live_client(sm, link_id, inner=fake.transport()) as http:
        with pytest.raises(SMTError) as e:
            await test_live_link(sm, link_id, actor=WHO, http=http)
    assert e.value.message == ETORO_CASH_ACCOUNT_MESSAGE
    assert e.value.details["field"] == "account_type"
    async with sm() as s:
        with pytest.raises(SMTError):
            await set_dry_run(s, link_id, True, actor=WHO)
    assert (await _actions(sm, link_id))[-1] == "test_failed"


async def _all_rows_text(sm) -> str:
    async with sm() as s:
        tables = list(await s.scalars(text(
            "select tablename from pg_tables where schemaname = 'public'")))
        out = []
        for t in tables:
            out.extend(await s.scalars(text(f'select row_to_json(x)::text from "{t}" x')))
    return "\n".join(out)


async def test_etoro_canary_keys_never_leak(sm, redis, fake, caplog):
    """Responses, live_audit, audit, outbox, broker_raw, logs: no key anywhere, also when
    eToro answers 401 echoing both keys."""
    caplog.set_level(logging.DEBUG)
    link_id, result = await _activation_flow(sm, redis, fake)
    seen = [repr(result)]
    inst = await _instrument(sm)
    async with sm() as s:  # a dry-run order through the adapter, kept with its raw answer
        await set_dry_run(s, link_id, True, actor=WHO)
        pid = await s.scalar(select(Portfolio.id).where(Portfolio.broker_link_id == link_id))
        order = Order(portfolio_id=pid, symbol="AAPL", side=OrderSide.buy,
                      type=OrderType.market, qty=D(1), broker_link_id=link_id)
        s.add(order)
        await s.commit()
    adapter = _adapter(sm, link_id, fake)
    with pytest.raises(LiveDryRun) as dry:
        await adapter.place_order(order, inst)
    seen.append(repr(dry.value.payload) + str(dry.value))
    await adapter.http.aclose()
    fake.echo_keys = True
    async with make_live_client(sm, link_id, inner=fake.transport()) as http:
        with pytest.raises(SMTError) as e:
            await test_live_link(sm, link_id, actor=WHO, http=http)
    seen.append(e.value.message + repr(e.value.details))
    assert "bad keys" in e.value.message
    text_all = "\n".join(seen) + "\n" + await _all_rows_text(sm) + "\n" + caplog.text
    for canary in CANARIES:
        assert canary not in text_all, canary
