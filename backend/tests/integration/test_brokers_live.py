"""Live adapters (alpaca_live, t212_live): built only on a LiveClient, never on another host,
plus the live-host guards (logs, secrets) and the test-only fake broker. Never reaches
the network: requests go to respx or to the in-memory FakeLiveBroker."""

import logging
import uuid
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers import registry
from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.live import LIVE_HOSTS, LIVE_KINDS
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.live.gate import LiveClient, make_live_client
from smtrading.brokers.live.t212_live import T212LiveAdapter
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.t212_demo import T212DemoAdapter
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.logsetup import RedactingFilter, redact_log_text, register_secret
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.models import Portfolio

ALPACA_CFG = {"key_id": "CANARYKEYID0001", "secret_key": "CANARYSECRET-do-not-use"}
T212_CFG = {"api_key": "CANARYKEYID0001", "api_secret": "CANARYSECRET-do-not-use"}
LIVE = [(AlpacaLiveAdapter, ALPACA_CFG), (T212LiveAdapter, T212_CFG)]
GATE_MESSAGE = "gli adattatori live richiedono il cancello"


@pytest.fixture
async def sm(migrated_db):
    """Sessions inside one rolled-back transaction (the gate's short sessions see the
    test's rows; nothing is committed)."""
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


async def _armed_link(sm, kind: str) -> uuid.UUID:
    async with sm() as s:
        link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=kind, live_state="armed")
        s.add(link)
        await s.flush()
        s.add(Portfolio(name=f"P-{uuid.uuid4().hex[:8]}", broker_link_id=link.id))
        await s.commit()
        return link.id


def _order(**kw) -> Order:
    base = {"id": uuid.uuid4(), "symbol": "AAPL", "side": OrderSide.buy,
            "type": OrderType.market, "tif": TimeInForce.day, "qty": Decimal(1)}
    base.update(kw)
    return Order(**base)


async def _stored(sm, link_id: uuid.UUID, order: Order) -> Order:
    """`order` saved in the link's portfolio (the gate looks the order up)."""
    async with sm() as s:
        if await s.get(Instrument, order.symbol) is None:
            s.add(_inst(order.symbol))
            await s.flush()
        order.portfolio_id = await s.scalar(
            select(Portfolio.id).where(Portfolio.broker_link_id == link_id))
        order.broker_link_id = link_id
        s.add(order)
        await s.commit()
    return order


def _inst(symbol="AAPL", asset_class=AssetClass.us_equity, currency="USD") -> Instrument:
    return Instrument(symbol=symbol, name=symbol, asset_class=asset_class, market="X",
                      currency=currency)


def _live_client() -> LiveClient:
    return make_live_client(None, uuid.uuid4(), inner=httpx.MockTransport(
        lambda r: httpx.Response(500)))


def test_registry_knows_the_live_kinds():
    assert {registry.adapter_class(k) for k in LIVE_KINDS} == {AlpacaLiveAdapter,
                                                               T212LiveAdapter}
    assert AlpacaLiveAdapter.live and T212LiveAdapter.live
    assert not AlpacaPaperAdapter.live and not T212DemoAdapter.live
    assert T212LiveAdapter.idempotent_place is False
    assert {AlpacaLiveAdapter.BASE_URL.split("//")[1],
            T212LiveAdapter.BASE_URL.split("//")[1]} == LIVE_HOSTS


@pytest.mark.parametrize(("cls", "cfg"), LIVE, ids=lambda v: getattr(v, "kind", ""))
async def test_live_rejects_plain_httpx_client(cls, cfg):
    async with httpx.AsyncClient() as plain:
        for http in (None, plain, object()):
            with pytest.raises(SMTError) as e:
                cls(dict(cfg), http)
            assert e.value.code == ErrorCode.FORBIDDEN
            assert e.value.message == GATE_MESSAGE
    client = _live_client()
    try:
        cls(dict(cfg), client)  # the gate's client: accepted
        client._transport = httpx.MockTransport(lambda r: httpx.Response(200))  # tampered
        with pytest.raises(SMTError) as e:
            cls(dict(cfg), client)
        assert e.value.code == ErrorCode.FORBIDDEN
    finally:
        await client.aclose()


@pytest.mark.parametrize(("cls", "cfg"), LIVE, ids=lambda v: getattr(v, "kind", ""))
async def test_live_rejects_other_base_url(cls, cfg):
    client = _live_client()
    try:
        for url in ("https://evil.example.com", "http://localhost:1",
                    AlpacaPaperAdapter.BASE_URL, T212DemoAdapter.BASE_URL):
            with pytest.raises(SMTError) as e:
                cls({**cfg, "base_url": url}, client)
            assert e.value.code == ErrorCode.FORBIDDEN
            assert e.value.message == f"{cls.kind}: base URL non consentito"
            assert url not in e.value.message
        assert cls({**cfg, "base_url": cls.BASE_URL + "/"}, client).BASE_URL == cls.BASE_URL
    finally:
        await client.aclose()


@pytest.mark.parametrize("cls", [AlpacaPaperAdapter, T212DemoAdapter], ids=lambda c: c.kind)
def test_paper_rejects_live_hosts(cls):
    for host in LIVE_HOSTS:
        for url in (f"https://{host}", f"https://{host.upper()}/", f"http://{host}:443/x",
                    host):
            with pytest.raises(SMTError) as e:
                cls({"base_url": url}, None)
            assert e.value.code == ErrorCode.FORBIDDEN
            assert e.value.message == "host di produzione non ammesso per un adattatore paper"
            assert host not in e.value.message.lower()


def test_alpaca_live_crypto_off_by_default():
    client = _live_client()
    btc = _inst("BTC-USD", AssetClass.crypto)
    order = _order(symbol="BTC-USD", qty=Decimal("0.01"), tif=TimeInForce.gtc)
    assert not AlpacaLiveAdapter(dict(ALPACA_CFG), client).supports(order, btc)
    for off in (False, "false", "", None, "no"):
        cfg = {**ALPACA_CFG, "crypto_enabled": off}
        assert not AlpacaLiveAdapter(cfg, client).supports(order, btc)
    for on in (True, "true", "TRUE"):
        cfg = {**ALPACA_CFG, "crypto_enabled": on}
        assert AlpacaLiveAdapter(cfg, client).supports(order, btc)
    assert AlpacaLiveAdapter(dict(ALPACA_CFG), client).supports(_order(), _inst())


def test_alpaca_live_notional_market_day_only():
    a = AlpacaLiveAdapter(dict(ALPACA_CFG), _live_client())
    inst = _inst()
    notional = {"qty": None, "notional": Decimal(50)}
    assert a.supports(_order(**notional), inst)
    assert not a.supports(_order(**notional, tif=TimeInForce.gtc), inst)
    assert not a.supports(_order(**notional, type=OrderType.limit,
                                 limit_price=Decimal(1)), inst)
    # fractional quantities: day only, never a trailing stop
    assert a.supports(_order(qty=Decimal("0.5")), inst)
    assert a.supports(_order(qty=Decimal("0.5"), type=OrderType.limit,
                             limit_price=Decimal(1)), inst)
    assert not a.supports(_order(qty=Decimal("0.5"), tif=TimeInForce.gtc), inst)
    assert not a.supports(_order(qty=Decimal("0.5"), type=OrderType.trailing_stop,
                                 trail_pct=Decimal(1)), inst)
    # whole shares: gtc allowed
    assert a.supports(_order(qty=Decimal(2), tif=TimeInForce.gtc), inst)
    assert a.supports(_order(qty=Decimal("2.000"), tif=TimeInForce.gtc), inst)


def test_t212_live_types_from_config():
    client = _live_client()
    default = T212LiveAdapter(dict(T212_CFG), client)
    assert default.supported_types == {OrderType.market, OrderType.limit, OrderType.stop,
                                       OrderType.stop_limit}
    narrow = T212LiveAdapter({**T212_CFG, "order_types": ["market", "limit"]}, client)
    assert narrow.supported_types == {OrderType.market, OrderType.limit}
    assert T212LiveAdapter({**T212_CFG, "order_types": "market, limit"},
                           client).supported_types == {OrderType.market, OrderType.limit}
    for bad in (["market", "trailing_stop"], ["iceberg"], []):
        with pytest.raises(SMTError) as e:
            T212LiveAdapter({**T212_CFG, "order_types": bad}, client)
        assert e.value.code == ErrorCode.INVALID_ORDER
    # quantity only, whatever the configuration
    narrow._instruments.data["items"] = []
    assert not narrow._supports_shape(_order(qty=None, notional=Decimal(5)), _inst())
    assert not narrow._supports_shape(_order(type=OrderType.stop, stop_price=Decimal(1)),
                                      _inst())
    assert narrow._supports_shape(_order(), _inst())


def test_httpx_logs_for_live_hosts_dropped():
    f = RedactingFilter()
    host = next(iter(LIVE_HOSTS))

    def rec(name: str, msg: str) -> logging.LogRecord:
        return logging.LogRecord(name, logging.WARNING, __file__, 1, msg, None, None)

    assert not f.filter(rec("httpx", f'HTTP Request: GET https://{host}/v2/account "200"'))
    assert not f.filter(rec("httpcore.connection", f"connect_tcp.started host='{host}'"))
    assert f.filter(rec("httpx", "HTTP Request: GET https://paper-api.example/v2 200"))
    assert f.filter(rec("smtrading.brokers", f"no live host filtering here: {host}"))


def test_registered_secret_redacted():
    register_secret("CANARYSECRET-do-not-use")
    register_secret("short")  # under 8 characters: never registered
    text = redact_log_text("failed with CANARYSECRET-do-not-use (short)")
    assert "CANARYSECRET" not in text and "<redacted>" in text
    assert "(short)" in text
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "v=%s",
                               ("CANARYSECRET-do-not-use",), None)
    assert RedactingFilter().filter(record)
    assert "CANARYSECRET" not in record.getMessage()


def _env(monkeypatch, migrated_db, fake: bool):
    monkeypatch.setenv("SMT_DATABASE_URL", migrated_db)
    monkeypatch.setenv("SMT_REDIS_URL", "redis://unused:6379/0")
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("SMT_LIVE_FAKE_BROKER", "true" if fake else "false")
    from smtrading.common.config import get_settings

    get_settings.cache_clear()


async def test_fake_broker_only_with_setting(sm, migrated_db, monkeypatch):
    _env(monkeypatch, migrated_db, fake=False)
    real = make_live_client(sm, uuid.uuid4())
    assert isinstance(real.gate._inner, httpx.AsyncHTTPTransport)
    await real.aclose()

    _env(monkeypatch, migrated_db, fake=True)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        alpaca_link = await _armed_link(sm, "alpaca_live")
        async with make_live_client(sm, alpaca_link) as http:
            assert isinstance(http.gate._inner, httpx.MockTransport)
            a = AlpacaLiveAdapter(dict(ALPACA_CFG), http)
            snap = await a.get_account()
            assert (snap.currency, snap.cash_available) == ("USD", Decimal("1000.00"))
            assert (snap.positions_count, snap.open_orders_count) == (0, 0)
            # the gate passes only orders of a linked portfolio (plan 9, Task 11)
            placed = await a.place_order(await _stored(sm, alpaca_link, _order()), _inst())
            assert placed.status == "filled" and placed.filled_qty == Decimal(1)
            assert await a.get_positions() == {"AAPL": Decimal(1)}
            limit = await a.place_order(await _stored(sm, alpaca_link, _order(
                type=OrderType.limit, limit_price=Decimal(1))), _inst())
            assert (await a.get_account()).open_orders_count == 1
            await a.cancel_order(limit.broker_order_id)
            assert (await a.get_order(limit.broker_order_id)).status == "canceled"

        t212_link = await _armed_link(sm, "t212_live")
        async with make_live_client(sm, t212_link) as http:
            t = T212LiveAdapter(dict(T212_CFG), http)
            snap = await t.get_account()
            assert (snap.currency, snap.cash_available) == ("EUR", Decimal("1000.00"))
            assert (snap.positions_count, snap.open_orders_count) == (0, 0)
            await t.health_check()
        assert not mock.calls  # the fake answered: no request reached the HTTP transport


def test_fake_broker_state_is_per_instance():
    a, b = FakeLiveBroker(), FakeLiveBroker()
    with httpx.Client(transport=a.transport()) as c:
        r = c.post("https://fake.invalid/v2/orders", json={
            "symbol": "AAPL", "side": "buy", "type": "market", "qty": "2",
            "time_in_force": "day", "client_order_id": "c1"})
        assert r.status_code == 200
        assert c.get("https://fake.invalid/v2/positions").json()[0]["qty"] == "2"
        too_much = c.post("https://fake.invalid/v2/orders", json={
            "symbol": "AAPL", "side": "buy", "type": "market", "qty": "1000",
            "time_in_force": "day", "client_order_id": "c2"})
        assert too_much.status_code == 403
    with httpx.Client(transport=b.transport()) as c:
        assert c.get("https://fake.invalid/v2/positions").json() == []
