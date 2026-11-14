"""Contract of the shared broker protocol cores (_AlpacaProtocol, _T212Protocol),
parametrized over the concrete adapter classes. Never reaches the network: respx mocks
every request (assert_all_mocked) and every mocked route must be hit (assert_all_called).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from smtrading.brokers import alpaca_core, t212_core
from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.base import AccountSnapshot, BrokerAdapter, RateLimited
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
from smtrading.brokers.live.gate import make_live_client
from smtrading.brokers.live.t212_live import T212LiveAdapter
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.t212_demo import T212DemoAdapter
from smtrading.common.db import make_engine
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

FIXTURES = Path(__file__).parent.parent / "fixtures" / "brokers"
T212_INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK", "currencyCode": "USD"},
]


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@dataclass(frozen=True)
class Case:
    cls: type[BrokerAdapter]
    family: str  # "alpaca" | "t212"
    config: dict

    @property
    def id(self) -> str:
        return self.cls.kind

    def adapter(self, http) -> BrokerAdapter:
        return self.cls(dict(self.config), http)

    def url(self, path: str) -> str:
        return self.cls.BASE_URL + path


ALPACA_CFG = {"key_id": "CANARYKEYID0001", "secret_key": "CANARYSECRET-do-not-use"}
T212_CFG = {"api_key": "CANARYKEYID0001", "api_secret": "CANARYSECRET-do-not-use"}
# The live classes run on a LiveClient (make_live_client) on an armed link of the test db.
CASES = [
    Case(AlpacaPaperAdapter, "alpaca", ALPACA_CFG),
    Case(T212DemoAdapter, "t212", T212_CFG),
    Case(AlpacaLiveAdapter, "alpaca", ALPACA_CFG),
    Case(T212LiveAdapter, "t212", T212_CFG),
]
ALPACA_CASES = [c for c in CASES if c.family == "alpaca"]
T212_CASES = [c for c in CASES if c.family == "t212"]


def _ids(cases):
    return [c.id for c in cases]


@pytest.fixture(autouse=True)
def _reset_t212_state():
    def reset():
        for cache in t212_core._instrument_caches.values():  # in place: modules alias them
            cache.data.update(at=0.0, items=None, failed_at=None)
            cache.reverse.clear()
        t212_core._rate_blocked_until.clear()

    reset()
    yield
    reset()


@pytest.fixture
async def http(request):
    """A plain client for the paper/demo classes; for a live class a LiveClient whose gate
    reads an armed link (with its one portfolio) inside a rolled-back transaction."""
    callspec = getattr(request.node, "callspec", None)
    case = callspec.params.get("case") if callspec is not None else None
    if case is None or not case.cls.live:
        async with httpx.AsyncClient() as c:
            yield c
        return
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.portfolios.models import Portfolio

    engine = make_engine(request.getfixturevalue("migrated_db"))
    async with engine.connect() as conn:
        trans = await conn.begin()
        sm = async_sessionmaker(bind=conn, expire_on_commit=False,
                                join_transaction_mode="create_savepoint")
        async with sm() as s:
            link = BrokerLink(name=f"L-{uuid.uuid4().hex[:8]}", kind=case.cls.kind,
                              live_state="armed")
            s.add(link)
            await s.flush()
            s.add(Portfolio(name=f"P-{uuid.uuid4().hex[:8]}", broker_link_id=link.id))
            await s.commit()
        try:
            async with make_live_client(sm, link.id,
                                        inner=httpx.AsyncHTTPTransport()) as c:
                yield c
        finally:
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
def mock():
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        yield router


def _inst(symbol="AAPL"):
    return Instrument(
        symbol=symbol, name=symbol, asset_class=AssetClass.us_equity, market="X", currency="USD"
    )


def _order(**kw):
    base = {
        "id": uuid.uuid4(),
        "symbol": "AAPL",
        "side": OrderSide.buy,
        "type": OrderType.market,
        "tif": TimeInForce.day,
        "qty": Decimal(10),
    }
    base.update(kw)
    return Order(**base)


def _t212_api(case: Case, path: str) -> str:
    return case.url(f"/api/v0/equity{path}")


def _mock_t212_instruments(mock, case: Case):
    mock.get(_t212_api(case, "/metadata/instruments")).mock(
        return_value=httpx.Response(200, json=T212_INSTRUMENTS)
    )


def _mock_place(mock, case: Case, response: httpx.Response):
    if case.family == "alpaca":
        return mock.post(case.url("/v2/orders")).mock(return_value=response)
    _mock_t212_instruments(mock, case)
    return mock.post(_t212_api(case, "/orders/market")).mock(return_value=response)


def _accepted(case: Case) -> httpx.Response:
    if case.family == "alpaca":
        return httpx.Response(200, json={"id": "b1", "status": "new", "filled_qty": "0"})
    return httpx.Response(200, json={"id": 42, "status": "NEW", "filledQuantity": 0})


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_place_payload_shape(case, http, mock):
    route = _mock_place(mock, case, _accepted(case))
    order = _order()
    res = await case.adapter(http).place_order(order, _inst())
    body = json.loads(route.calls[0].request.content)
    if case.family == "alpaca":
        assert body == {
            "symbol": "AAPL",
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": str(order.id),
            "qty": "10",
        }
        assert res.broker_order_id == "b1"
    else:
        assert body == {"ticker": "AAPL_US_EQ", "quantity": 10}
        assert res.broker_order_id == "42"
    assert res.status == "accepted"
    assert res.fees is None


@pytest.mark.parametrize(
    ("case", "raw", "expected"),
    [(c, raw, exp) for c in ALPACA_CASES for raw, exp in alpaca_core._STATUS.items()]
    + [(c, raw, exp) for c in T212_CASES for raw, exp in t212_core._STATUS.items()],
    ids=lambda v: v.id if isinstance(v, Case) else str(v),
)
async def test_status_mapping(case, raw, expected, http, mock):
    if case.family == "alpaca":
        mock.get(case.url("/v2/orders/x1")).mock(
            return_value=httpx.Response(200, json={"id": "x1", "status": raw})
        )
        bo = await case.adapter(http).get_order("x1")
    else:
        mock.get(_t212_api(case, "/orders/11")).mock(
            return_value=httpx.Response(200, json={"id": 11, "status": raw})
        )
        bo = await case.adapter(http).get_order("11")
    assert bo.status == expected


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_find_order_contract(case, http, mock):
    """The submission's broker order is found; the same order excluded is not found."""
    order = _order()
    since = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    if case.family == "alpaca":
        mock.get(
            case.url("/v2/orders:by_client_order_id"),
            params={"client_order_id": str(order.id)},
        ).mock(
            return_value=httpx.Response(
                200, json={"id": "b7", "status": "filled", "filled_qty": "10"}
            )
        )
    else:
        _mock_t212_instruments(mock, case)
        mock.get(_t212_api(case, "/orders")).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "ticker": "AAPL_US_EQ",
                        "quantity": 10,
                        "status": "NEW",
                        "createdAt": "2026-11-14T10:00:05Z",
                    }
                ],
            )
        )
        mock.get(_t212_api(case, "/history/orders"), params={"limit": "50"}).mock(
            return_value=httpx.Response(200, json={"items": [], "nextPagePath": None})
        )
    a = case.adapter(http)
    found = await a.find_order(order, _inst(), since)
    expected_id = "b7" if case.family == "alpaca" else "7"
    assert found is not None
    assert found.broker_order_id == expected_id
    assert await a.find_order(order, _inst(), since, frozenset({expected_id})) is None


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_get_account_snapshot(case, http, mock):
    if case.family == "alpaca":
        acct = mock.get(case.url("/v2/account")).mock(
            return_value=httpx.Response(200, json=_fixture("alpaca_account.json"))
        )
        mock.get(case.url("/v2/positions")).mock(
            return_value=httpx.Response(200, json=[{"symbol": "AAPL", "qty": "1"}])
        )
        orders = mock.get(case.url("/v2/orders"), params={"status": "open"}).mock(
            return_value=httpx.Response(200, json=[])
        )
    else:
        acct = mock.get(_t212_api(case, "/account/summary")).mock(
            return_value=httpx.Response(200, json=_fixture("t212_account_summary.json"))
        )
        mock.get(_t212_api(case, "/positions")).mock(
            return_value=httpx.Response(
                200, json=[{"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 1}]
            )
        )
        orders = mock.get(_t212_api(case, "/orders")).mock(
            return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        )
    snap = await case.adapter(http).get_account()
    assert isinstance(snap, AccountSnapshot)
    assert snap.positions_count == 1
    if case.family == "alpaca":
        assert snap == AccountSnapshot(
            account_id="e6fe16f3-64a4-4921-8928-cadf02f92f98",
            account_type="cash",
            currency="USD",
            cash_available=Decimal("1000.50"),
            positions_count=1,
            open_orders_count=0,
            crypto_enabled=True,
            order_types=None,
            raw=_fixture("alpaca_account.json"),
        )
    else:
        assert snap.account_id == "20001234"
        assert snap.account_type is None
        assert snap.currency == "EUR"
        assert snap.cash_available == Decimal("987.65")
        assert snap.open_orders_count == 2
        assert snap.crypto_enabled is None
        assert snap.raw["cash"]["availableToTrade"] == "987.65"
    # read-only: every request of the snapshot is a GET
    assert all(c.request.method == "GET" for c in mock.calls)
    assert acct.called and orders.called


@pytest.mark.parametrize("case", ALPACA_CASES, ids=_ids(ALPACA_CASES))
async def test_alpaca_account_cash_capped_by_buying_power(case, http, mock):
    data = {
        **_fixture("alpaca_account.json"),
        "cash": "500",
        "buying_power": "120.5",
        "crypto_status": "INACTIVE",
        "multiplier": "2",
    }
    mock.get(case.url("/v2/account")).mock(return_value=httpx.Response(200, json=data))
    mock.get(case.url("/v2/positions")).mock(return_value=httpx.Response(200, json=[]))
    mock.get(case.url("/v2/orders")).mock(return_value=httpx.Response(200, json=[]))
    snap = await case.adapter(http).get_account()
    assert snap.cash_available == Decimal("120.5")
    assert snap.account_type == "margin"
    assert snap.crypto_enabled is False


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_get_account_rejects_malformed_response(case, http, mock):
    path = "/v2/account" if case.family == "alpaca" else "/api/v0/equity/account/summary"
    mock.get(case.url(path)).mock(return_value=httpx.Response(200, json={"currency": "EUR"}))
    with pytest.raises(RuntimeError, match="unexpected account response"):
        await case.adapter(http).get_account()


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_order_requests_carry_order_id_extension(case, http, mock):
    route = _mock_place(mock, case, _accepted(case))
    order = _order()
    await case.adapter(http).place_order(order, _inst())
    assert route.calls[0].request.extensions["smt_order_id"] == str(order.id)
    if case.family == "t212":  # metadata reads are not order requests
        meta = next(c for c in mock.calls if c.request.method == "GET")
        assert "smt_order_id" not in meta.request.extensions


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_ratelimit_waits_for_reset(case, http, mock):
    now = 1_800_000_000.0
    route = mock.get(_t212_api(case, "/positions")).mock(
        side_effect=[
            httpx.Response(
                200,
                json=[],
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 5)},
            ),
            httpx.Response(
                200,
                json=[],
                headers={"x-ratelimit-remaining": "9", "x-ratelimit-reset": str(int(now) + 60)},
            ),
            httpx.Response(200, json=[]),
        ]
    )
    _mock_t212_instruments(mock, case)
    a = case.adapter(http)
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    a._sleep = fake_sleep
    a._clock = lambda: now
    await a.health_check()
    assert slept == []
    await a.get_positions()  # the metadata endpoint has its own limit: no wait for it
    assert slept == [5.0]
    await a.health_check()  # remaining was 9: no further wait
    assert slept == [5.0]
    assert route.call_count == 3


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_ratelimit_state_shared_across_instances(case, http, mock):
    now = 1_800_000_000.0
    mock.get(_t212_api(case, "/positions")).mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(now * 1000) + 2000),
            },
        )
    )
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    first, second = case.adapter(http), case.adapter(http)
    for a in (first, second):
        a._sleep = fake_sleep
        a._clock = lambda: now
    await first.health_check()
    await second.health_check()  # same account: waits (reset given in milliseconds)
    assert slept == [2.0]


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_429_is_retryable_not_rejected(case, http, mock):
    _mock_place(mock, case, httpx.Response(429, json={"code": "TooManyRequests"}))
    with pytest.raises(RuntimeError, match="limite di frequenza Trading 212") as e:
        await case.adapter(http).place_order(_order(), _inst())
    assert isinstance(e.value, RateLimited)
    assert "CANARY" not in str(e.value)


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
async def test_fees_parsed_when_present(case, http, mock):
    if case.family == "alpaca":
        mock.get(case.url("/v2/orders/s1")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "s1",
                    "status": "filled",
                    "filled_qty": "2",
                    "filled_avg_price": "190.25",
                    "commission": "0.02",
                },
            )
        )
        bo = await case.adapter(http).get_order("s1")
        assert bo.fees == Decimal("0.02")
    else:
        mock.get(_t212_api(case, "/orders/77")).mock(return_value=httpx.Response(404))
        mock.get(_t212_api(case, "/history/orders"), params={"limit": "50"}).mock(
            return_value=httpx.Response(200, json=_fixture("t212_history_fill_with_fees.json"))
        )
        bo = await case.adapter(http).get_order("77")
        assert bo.status == "filled"
        assert bo.avg_fill_price == Decimal("190.25")
        assert bo.fees == Decimal("0.54")


class _OtherT212(t212_core._T212Protocol):
    """A second Trading 212 environment (test only, not registered)."""

    kind = "t212_other_env"
    BASE_URL = "https://other-env.t212.invalid"


async def test_t212_instrument_cache_is_per_environment(http, mock):
    """A failure/backoff in one environment never affects another, and ticker lists
    never cross environments."""
    demo_meta = mock.get(T212DemoAdapter.BASE_URL + "/api/v0/equity/metadata/instruments").mock(
        return_value=httpx.Response(503, json={})
    )
    other_meta = mock.get(_OtherT212.BASE_URL + "/api/v0/equity/metadata/instruments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"ticker": "SAPd_EQ", "shortName": "SAP", "type": "STOCK", "currencyCode": "EUR"},
            ],
        )
    )
    demo, other = T212DemoAdapter(dict(T212_CFG), http), _OtherT212(dict(T212_CFG), http)
    with pytest.raises(RuntimeError):
        await demo.prepare()
    with pytest.raises(RuntimeError, match="backing off"):
        await demo.prepare()
    await other.prepare()  # not blocked by the demo backoff
    assert demo_meta.call_count == 1 and other_meta.call_count == 1
    sap = Instrument(
        symbol="SAP.DE", name="SAP", asset_class=AssetClass.eu_equity, market="X", currency="EUR"
    )
    assert other.supports(_order(symbol="SAP.DE"), sap)
    assert not demo.supports(_order(symbol="SAP.DE"), sap)  # no metadata in demo
    assert t212_core.instrument_cache(_OtherT212.BASE_URL).reverse == {"SAPd_EQ": "SAP.DE"}
    assert t212_core.instrument_cache(T212DemoAdapter.BASE_URL).reverse == {}


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_ratelimit_keeps_a_newer_block_set_while_sleeping(case, http):
    now = 1_800_000_000.0
    a = case.adapter(http)
    a._clock = lambda: now
    key = a._rate_key("GET", "/api/v0/equity/positions")

    async def concurrent_sleep(s: float) -> None:  # another request exhausts it again
        t212_core._rate_blocked_until[key] = now + 30

    a._sleep = concurrent_sleep
    t212_core._rate_blocked_until[key] = now + 5
    await a._respect_rate_limit(key)
    assert t212_core._rate_blocked_until[key] == now + 30


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_find_order_requests_carry_order_id(case, http, mock):
    _mock_t212_instruments(mock, case)
    mock.get(_t212_api(case, "/orders")).mock(return_value=httpx.Response(200, json=[]))
    mock.get(_t212_api(case, "/history/orders"), params={"limit": "50"}).mock(
        return_value=httpx.Response(200, json={"items": [], "nextPagePath": None})
    )
    order = _order()
    await case.adapter(http).find_order(order, _inst(), datetime(2026, 11, 14, tzinfo=UTC))
    lookups = [c.request for c in mock.calls if "/metadata/" not in c.request.url.path]
    assert len(lookups) == 2
    assert all(r.extensions["smt_order_id"] == str(order.id) for r in lookups)


@pytest.mark.parametrize("case", T212_CASES, ids=_ids(T212_CASES))
async def test_t212_foreign_currency_taxes_excluded_and_reported(case, http, mock, caplog):
    page = _fixture("t212_history_fill_with_fees.json")
    taxes = page["items"][0]["fill"]["walletImpact"]["taxes"]
    taxes.append({"name": "FINRA_FEE", "quantity": -0.02, "currency": "USD"})
    mock.get(_t212_api(case, "/orders/77")).mock(return_value=httpx.Response(404))
    mock.get(_t212_api(case, "/history/orders"), params={"limit": "50"}).mock(
        return_value=httpx.Response(200, json=page)
    )
    with caplog.at_level("WARNING", logger="smtrading.brokers.t212"):
        bo = await case.adapter(http).get_order("77")
    assert bo.fees == Decimal("0.54")  # the USD tax is not converted nor summed
    assert bo.raw["smt_unconverted_taxes"] == [
        {"name": "FINRA_FEE", "quantity": "-0.02", "currency": "USD"}
    ]
    assert any("not in the account currency" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_other_base_url_forbidden_without_host_in_message(case):
    from smtrading.common.errors import ErrorCode, SMTError

    with pytest.raises(SMTError) as e:
        case.cls({**case.config, "base_url": "https://evil.example.com"}, None)
    assert e.value.code == ErrorCode.FORBIDDEN
    assert str(e.value.message) == f"{case.cls.kind}: base URL non consentito"
    assert case.cls.BASE_URL not in str(e.value.message)
