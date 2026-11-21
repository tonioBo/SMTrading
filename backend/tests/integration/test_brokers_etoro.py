"""eToro demo adapter (Task 13 of plan 9), on the FakeLiveBroker transport or respx.

Never reaches the network. The real execution paths are never written in this file: a
test that needs one derives it from the demo class constants.
"""

import json
import socket
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from smtrading.brokers import etoro_core
from smtrading.brokers.base import BrokerAdapter, RateLimited
from smtrading.brokers.etoro_core import (
    ETORO_NS,
    ETORO_URL,
    MARKET_DATA_PREFIX,
    MARKET_ONLY_MESSAGE,
    NOT_CLOSABLE_MESSAGE,
    NOT_TRADABLE_MESSAGE,
    QTY_ONLY_MESSAGE,
    etoro_headers,
    http_error,
    instrument_cache,
    rate_headers,
    search_instrument,
)
from smtrading.brokers.etoro_demo import EtoroDemoAdapter
from smtrading.brokers.live import hostname
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.registry import adapter_class
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

FIXTURES = Path(__file__).parent.parent / "fixtures" / "brokers"
CFG = {"api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-do-not-use"}
A = EtoroDemoAdapter
PNL = ETORO_URL + A.INFO_V1 + "/pnl"
SEARCH = ETORO_URL + MARKET_DATA_PREFIX + "search"
ELIGIBILITY = ETORO_URL + A.INFO_V2 + "/eligibility"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _inst(symbol="AAPL", ac=AssetClass.us_equity, cur="USD", provider_symbols=None):
    return Instrument(symbol=symbol, name=symbol, asset_class=ac, market="X", currency=cur,
                      provider_symbols=provider_symbols or {})


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


@pytest.fixture
def fake():
    return FakeLiveBroker(price=Decimal(100))


@pytest.fixture
async def sent():
    """Every request the client sent (headers and bodies)."""
    return []


@pytest.fixture
async def http(fake, sent):
    async def record(request: httpx.Request) -> None:
        sent.append(request)

    async with httpx.AsyncClient(transport=fake.transport(),
                                 event_hooks={"request": [record]}) as c:
        yield c


@pytest.fixture
def adapter(http):
    return EtoroDemoAdapter(dict(CFG), http)


@pytest.fixture
async def plain():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def mock():
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        yield router


def _bodies(sent, *, exclude_eligibility=True) -> list[dict]:
    out = []
    for r in sent:
        if r.method != "POST" or (exclude_eligibility and r.url.path.endswith("/eligibility")):
            continue
        out.append(json.loads(r.content))
    return out


def _search_item(symbol="AAPL", iid=1001, **kw):
    item = {"instrumentId": iid, "internalSymbolFull": symbol, "isCurrentlyTradable": True,
            "isBuyEnabled": True, "isDelisted": False}
    item.update(kw)
    return item


def _eligibility(iid=1001, *, allow_open=True, partial=True, configs=None):
    if configs is None:
        configs = [{"settlementType": "real", "direction": "long", "leverageValues": [1]}]
    # the real eToro shape (seen on the demo API 21/11/2026)
    return {"currency": "usd", "eligibilities": [{"instrumentId": iid, "allowOpenPosition": allow_open,
                             "allowPartialClosePosition": partial,
                             "leverageConfigs": configs}]}


async def _buy(adapter, qty, symbol="AAPL"):
    order = _order(symbol=symbol, qty=Decimal(qty))
    bo = await adapter.place_order(order, _inst(symbol))
    assert bo.status == "accepted", bo
    return order, bo


def test_registered_and_class_constants():
    assert adapter_class("etoro_demo") is EtoroDemoAdapter
    assert issubclass(EtoroDemoAdapter, BrokerAdapter)
    assert A.kind == "etoro_demo" and A.ENV == "demo" and A.BASE_URL == ETORO_URL
    assert A.live is False and A.idempotent_place is False
    assert A.supported_types == frozenset({OrderType.market})
    for prefix in (A.EXEC_V1, A.EXEC_V2, A.INFO_V1, A.INFO_V2):
        assert "/demo" in prefix
    assert all("/demo/" in p or p == MARKET_DATA_PREFIX for p in A.ALLOWED_PREFIXES)
    assert isinstance(ETORO_NS, uuid.UUID)


async def test_buy_payload_is_unleveraged_buy(adapter, fake, sent):
    order, bo = await _buy(adapter, 10)
    post = next(r for r in sent if r.method == "POST" and r.url.path.endswith("/orders"))
    assert post.url.path == A.EXEC_V2 + "/orders"
    assert json.loads(post.content) == {
        "action": "open", "transaction": "buy", "instrumentId": 1001,
        "settlementType": "real", "orderType": "mkt", "leverage": 1, "units": 10,
    }
    assert post.headers["x-api-key"] == CFG["api_key"]
    assert post.headers["x-user-key"] == CFG["user_key"]
    assert post.headers["x-request-id"] == str(uuid.uuid5(ETORO_NS, BrokerAdapter.client_ref(order)))
    assert post.extensions["smt_order_id"] == str(order.id)
    # the other requests carry a fresh request id each
    others = [r.headers["x-request-id"] for r in sent if r is not post]
    assert others and len(set(others)) == len(others)
    assert post.headers["x-request-id"] not in others
    got = await adapter.get_order(bo.broker_order_id)
    assert (got.status, got.filled_qty, got.avg_fill_price) == ("filled", Decimal(10), Decimal(100))
    assert got.fees == Decimal(0)


@pytest.mark.parametrize("case", ["buy_units", "buy_amount", "sell_whole", "sell_partial"])
async def test_payload_never_short_or_leveraged(case, adapter, fake, sent):
    await _buy(adapter, 2)
    await _buy(adapter, 2)
    if case == "buy_amount":
        bo = await adapter.place_order(_order(qty=None, notional=Decimal(150)), _inst())
        assert bo.status == "accepted"
    elif case.startswith("sell"):
        qty = Decimal(4) if case == "sell_whole" else Decimal(3)
        bo = await adapter.place_order(_order(side=OrderSide.sell, qty=qty), _inst())
        assert bo.status == "accepted"
    bodies = _bodies(sent)
    assert bodies
    for body in bodies:
        text = json.dumps(body).lower()
        assert "sellshort" not in text and "short" not in text
        assert body.get("leverage", 1) == 1
        assert body.get("settlementType", "real") == "real"
        assert body.get("transaction", "buy") == "buy"
        assert body.get("action", "open") == "open"
        assert set(body) <= {"action", "transaction", "instrumentId", "settlementType",
                             "orderType", "leverage", "units", "amount",
                             "InstrumentID", "UnitsToDeduct"}


async def test_notional_amount_only_usd(adapter, fake, sent):
    bo = await adapter.place_order(_order(qty=None, notional=Decimal("250.50")), _inst())
    assert bo.status == "accepted"
    body = _bodies(sent)[0]
    assert body["amount"] == 250.5 and "units" not in body
    sap = _inst("SAP.DE", AssetClass.eu_equity, "EUR", {"etoro": "SAP.DE"})
    eur_notional = _order(symbol="SAP.DE", qty=None, notional=Decimal(100))
    assert adapter.routable(eur_notional, sap) is False
    assert adapter.unsupported_reason(eur_notional, sap) == QTY_ONLY_MESSAGE
    before = len(fake.calls)
    bo = await adapter.place_order(eur_notional, sap)
    assert (bo.status, bo.message) == ("rejected", QTY_ONLY_MESSAGE)
    assert not [c for c in list(fake.calls)[before:] if c[0] == "POST"]
    # a sell by amount cannot be a position close
    sell_amount = _order(side=OrderSide.sell, qty=None, notional=Decimal(100))
    assert adapter.unsupported_reason(sell_amount, _inst()) == QTY_ONLY_MESSAGE


@pytest.mark.parametrize(
    "otype", [OrderType.limit, OrderType.stop, OrderType.stop_limit, OrderType.trailing_stop]
)
async def test_non_market_types_not_routable_message(otype, adapter, fake):
    order = _order(type=otype, limit_price=Decimal(90), stop_price=Decimal(95))
    assert adapter.routable(order, _inst()) is False
    assert adapter.unsupported_reason(order, _inst()) == MARKET_ONLY_MESSAGE
    assert MARKET_ONLY_MESSAGE == (
        "tipo d'ordine non disponibile su questo broker: solo ordini a mercato")
    bo = await adapter.place_order(order, _inst())
    assert (bo.status, bo.message) == ("rejected", MARKET_ONLY_MESSAGE)
    assert not [c for c in fake.calls if c[0] == "POST"]
    # a market order on the same instrument is routable (no network at routing)
    assert adapter.unsupported_reason(_order(), _inst()) is None


async def _three_positions(adapter, fake):
    for _ in range(3):
        await _buy(adapter, 1)
    ids = sorted(fake.etoro["demo"].positions)
    assert len(ids) == 3
    return ids


async def test_sell_fifo_partial_close_allowed(adapter, fake, sent):
    ids = await _three_positions(adapter, fake)
    order = _order(side=OrderSide.sell, qty=Decimal("2.5"))
    bo = await adapter.place_order(order, _inst())
    closes = [r for r in sent if "market-close-orders" in r.url.path]
    assert [r.url.path for r in closes] == [
        f"{A.EXEC_V1}/market-close-orders/positions/{pid}" for pid in ids]
    assert [json.loads(r.content) for r in closes] == [
        {"InstrumentID": 1001, "UnitsToDeduct": None},
        {"InstrumentID": 1001, "UnitsToDeduct": None},
        {"InstrumentID": 1001, "UnitsToDeduct": 0.5},
    ]
    assert [r.extensions["smt_close"] for r in closes] == [
        {"symbol": "AAPL", "instrument_id": 1001, "units": "1"},
        {"symbol": "AAPL", "instrument_id": 1001, "units": "1"},
        {"symbol": "AAPL", "instrument_id": 1001, "units": "0.5"},
    ]
    assert all(r.extensions["smt_order_id"] == str(order.id) for r in closes)
    assert bo.status == "accepted" and bo.broker_order_id.startswith("close:2.5:")
    got = await adapter.get_order(bo.broker_order_id)
    assert (got.status, got.filled_qty, got.avg_fill_price) == ("filled", Decimal("2.5"),
                                                                Decimal(100))
    assert await adapter.get_positions() == {"AAPL": Decimal("0.5")}
    # a sell has no reference to look up
    assert await adapter.find_order(order, _inst(), datetime.now(UTC)) is None
    await adapter.cancel_order(bo.broker_order_id)  # market closes: nothing to cancel


async def test_sell_without_partial_close_closes_whole_positions_rest_partial(
    adapter, fake, sent
):
    fake.etoro_partial_close = False
    ids = await _three_positions(adapter, fake)
    bo = await adapter.place_order(_order(side=OrderSide.sell, qty=Decimal("2.5")), _inst())
    closes = [r for r in sent if "market-close-orders" in r.url.path]
    assert [json.loads(r.content)["UnitsToDeduct"] for r in closes] == [None, None]
    assert [r.url.path.rsplit("/", 1)[1] for r in closes] == [str(i) for i in ids[:2]]
    got = await adapter.get_order(bo.broker_order_id)
    assert got.status == "canceled"
    assert got.filled_qty == Decimal(2) and got.avg_fill_price == Decimal(100)
    assert got.message is None  # nothing broker-specific towards the agents
    assert await adapter.get_positions() == {"AAPL": Decimal(1)}


async def test_sell_nothing_closable_is_rejected(adapter, fake):
    fake.etoro_partial_close = False
    await _buy(adapter, 2)
    bo = await adapter.place_order(_order(side=OrderSide.sell, qty=Decimal(1)), _inst())
    assert (bo.status, bo.message) == ("rejected", NOT_CLOSABLE_MESSAGE)
    assert not [c for c in fake.calls if "market-close-orders" in c[1]]


async def test_sell_error_after_first_close_keeps_accepted_closes(adapter, fake):
    ids = await _three_positions(adapter, fake)
    fake.etoro_fail_close_after = 1  # the second close fails
    bo = await adapter.place_order(_order(side=OrderSide.sell, qty=Decimal(3)), _inst())
    assert bo.status == "accepted"
    assert len(bo.broker_order_id.split(":")[2].split(",")) == 1
    got = await adapter.get_order(bo.broker_order_id)
    assert (got.status, got.filled_qty) == ("canceled", Decimal(1))
    assert sorted(fake.etoro["demo"].positions) == ids[1:]


def test_status_mapping_table():
    assert etoro_core._STATUS == {
        1: "accepted", 2: "accepted", 3: "filled", 4: "rejected", 5: "partially_filled",
        6: "accepted", 7: "canceled", 8: "expired", 9: "canceled", 10: "canceled",
        11: "accepted", 12: "accepted",
    }


@pytest.mark.parametrize(
    "asset", [{"leverage": 2, "settlementType": "real"}, {"leverage": 1, "settlementType": "cfd"}]
)
async def test_lookup_leveraged_or_unreal_order_is_rejected(asset, plain, mock, caplog):
    data = {**_fixture("etoro_order_lookup.json"),
            "asset": {"instrumentId": 1001, "side": "buy", **asset}}
    mock.get(ETORO_URL + A.INFO_V2 + "/orders:lookup", params={"orderId": "77"}).mock(
        return_value=httpx.Response(200, json=data))
    with caplog.at_level("ERROR", logger="smtrading.brokers.etoro"):
        bo = await EtoroDemoAdapter(dict(CFG), plain).get_order("77")
    assert bo.status == "rejected"
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", A.EXEC_V2.replace("/demo", "") + "/orders"),
        ("GET", A.INFO_V2.replace("/demo", "") + "/orders:lookup?orderId=1"),
        ("POST", A.EXEC_V1.replace("/demo", "") + "/market-close-orders/positions/1"),
        ("GET", A.INFO_V1.replace("/demo", "/real") + "/pnl"),
        ("POST", MARKET_DATA_PREFIX + "search"),
        ("GET", "/api/v1/other/thing"),
        ("GET", A.INFO_V1 + "/../real/pnl"),
        ("GET", A.INFO_V1 + "//pnl"),
        ("GET", A.INFO_V1 + "/%2e%2e/real/pnl"),
        ("GET", A.INFO_V1 + "/%252e%252e/real/pnl"),  # double-encoded
        ("GET", A.INFO_V1 + "/%2E%2E/real/pnl"),  # uppercase
        ("GET", A.INFO_V1 + "%2F..%2Freal/pnl"),
        ("GET", A.INFO_V1 + "/pnl%20"),
        ("GET", A.INFO_V1 + "\\..\\real/pnl"),
        ("GET", A.INFO_V1 + "/close-orders/1%3F"),
    ],
)
async def test_demo_adapter_cannot_call_real_path(method, path, adapter, fake):
    before = list(fake.calls)
    with pytest.raises(SMTError) as e:
        await adapter._request(method, path, body={"x": 1} if method == "POST" else None)
    assert e.value.code == ErrorCode.FORBIDDEN
    assert e.value.message == "percorso non ammesso per questo collegamento"
    assert list(fake.calls) == before


def test_fake_calls_are_bounded(fake):
    assert fake.calls.maxlen == 1000


async def test_demo_paths_all_contain_demo(adapter, fake):
    order, bo = await _buy(adapter, 2)
    await adapter.get_order(bo.broker_order_id)
    await adapter.find_order(order, _inst(), datetime.now(UTC))
    await adapter.cancel_order(bo.broker_order_id)
    sell = await adapter.place_order(_order(side=OrderSide.sell, qty=Decimal(1)), _inst())
    await adapter.get_order(sell.broker_order_id)
    await adapter.get_positions()
    await adapter.get_account()
    await adapter.positions_after_account()
    await adapter.health_check()
    trading = [(m, p) for m, p in fake.calls if not p.startswith(MARKET_DATA_PREFIX)]
    assert len(trading) >= 8
    assert all("/demo/" in p for _, p in trading), trading
    assert all(m == "GET" for m, p in fake.calls if p.startswith(MARKET_DATA_PREFIX))


async def test_account_snapshot_cash_and_empty(adapter, fake, plain, mock):
    snap = await adapter.get_account()
    assert (snap.currency, snap.cash_available, snap.positions_count,
            snap.open_orders_count, snap.account_type) == ("USD", Decimal("1000.00"), 0, 0,
                                                           "cash")
    assert snap.order_types == frozenset({OrderType.market})
    assert snap.account_id == etoro_core.account_digest(CFG["user_key"])
    assert CFG["user_key"] not in json.dumps(snap.raw)
    mock.get(PNL).mock(return_value=httpx.Response(200, json=_fixture("etoro_pnl.json")))
    snap = await EtoroDemoAdapter(dict(CFG), plain).get_account()
    # credit - ordersForOpen (own, mirrorID 0) - orders: 1000 - 100 - 25
    assert snap.cash_available == Decimal(875)
    assert snap.positions_count == 1 and snap.open_orders_count == 3
    assert snap.account_type == "cash"
    assert all(c.request.method == "GET" for c in mock.calls)


async def test_account_currency_not_usd_fails(plain, mock):
    data = _fixture("etoro_pnl.json")
    data["clientPortfolio"]["accountCurrencyId"] = 2
    mock.get(PNL).mock(return_value=httpx.Response(200, json=data))
    with pytest.raises(RuntimeError, match="valuta del conto non supportata"):
        await EtoroDemoAdapter(dict(CFG), plain).get_account()


@pytest.mark.parametrize(
    "change",
    [{"leverage": 2}, {"isBuy": False}, {"settlementTypeID": 2}, {"mirrorID": 12},
     {"mirrorID": None}],  # None: the field is left out (fail-closed)
)
async def test_nonconforming_position_marks_leveraged(change, plain, mock):
    data = _fixture("etoro_pnl.json")
    positions = data["clientPortfolio"]["positions"]
    # the second position is in the documented example casing (positionId / instrumentId)
    positions.append({"positionId": 502, "instrumentId": 1002, "isBuy": True, "units": 3,
                      "openRate": 400, "amount": 1200, "leverage": 1, "mirrorId": 0,
                      "settlementTypeId": 1, **{k.replace("ID", "Id"): v
                                                for k, v in change.items()}})
    positions[-1] = {k: v for k, v in positions[-1].items() if v is not None}
    mock.get(PNL).mock(return_value=httpx.Response(200, json=data))
    a = EtoroDemoAdapter(dict(CFG), plain)
    snap = await a.get_account()
    assert snap.account_type == "leveraged"
    assert snap.positions_count == 2
    assert await a.get_positions() == {"1001": Decimal(2)}


async def test_find_order_by_reference_id(adapter, fake, sent):
    order, bo = await _buy(adapter, 3)
    since = datetime.now(UTC)
    found = await adapter.find_order(order, _inst(), since)
    assert found is not None and found.broker_order_id == bo.broker_order_id
    assert found.status == "filled"
    lookup = sent[-1]
    assert lookup.url.path == A.INFO_V2 + "/orders:lookup"
    assert lookup.url.params["referenceId"] == str(
        uuid.uuid5(ETORO_NS, BrokerAdapter.client_ref(order)))
    assert lookup.extensions["smt_order_id"] == str(order.id)
    assert await adapter.find_order(order, _inst(), since,
                                    frozenset({bo.broker_order_id})) is None
    assert await adapter.find_order(_order(), _inst(), since) is None  # never sent
    # a new submission of the same order has a new reference
    order.broker_attempt = 1
    assert await adapter.find_order(order, _inst(), since) is None


async def test_ratelimit_waits_and_429_retryable(plain, mock):
    now = 1_800_000_000.0
    route = mock.get(PNL).mock(side_effect=[
        httpx.Response(200, json=_fixture("etoro_pnl.json"),
                       headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "5"}),
        httpx.Response(429, json={"errorMessage": "Too many requests"},
                       headers={"Retry-After": "7"}),
        httpx.Response(200, json=_fixture("etoro_pnl.json")),
    ])
    a = EtoroDemoAdapter(dict(CFG), plain)
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    a._sleep, a._clock = fake_sleep, lambda: now
    await a.health_check()
    assert slept == []
    with pytest.raises(RateLimited) as e:
        await a.health_check()
    assert slept == [5.0]
    assert CFG["user_key"] not in str(e.value) and CFG["api_key"] not in str(e.value)
    await a.health_check()  # waits Retry-After, at most 60 s
    assert slept == [5.0, 7.0]
    assert route.call_count == 3


async def test_ratelimit_wait_capped(plain, mock):
    mock.get(PNL).mock(side_effect=[
        httpx.Response(200, json=_fixture("etoro_pnl.json"),
                       headers={"RateLimit-Remaining": "0", "RateLimit-Reset": "600"}),
        httpx.Response(200, json=_fixture("etoro_pnl.json")),
    ])
    a = EtoroDemoAdapter(dict(CFG), plain)
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    a._sleep, a._clock = fake_sleep, lambda: 1_800_000_000.0
    await a.health_check()
    await a.health_check()
    assert slept == [60.0]


def test_rate_headers_and_http_error():
    ok = httpx.Response(200, headers={"RateLimit-Remaining": "3", "RateLimit-Reset": "12"})
    assert rate_headers(ok) == (3, 12.0)
    assert rate_headers(httpx.Response(200)) == (None, None)
    limited = httpx.Response(429, headers={"Retry-After": "9"})
    assert rate_headers(limited) == (0, 9.0)
    from smtrading.common.logsetup import register_secret

    register_secret(CFG["user_key"])
    resp = httpx.Response(
        400, json={"errorMessage": f"bad key {CFG['user_key']}"},
        request=httpx.Request("POST", ETORO_URL, headers=etoro_headers(**_keys())))
    text = http_error(resp)
    assert text.startswith("etoro: HTTP 400")
    assert CFG["user_key"] not in text and CFG["api_key"] not in text
    assert http_error(httpx.Response(503, content=b"<html>")) == "etoro: HTTP 503"


def _keys():
    return {"api_key": CFG["api_key"], "user_key": CFG["user_key"]}


def test_etoro_headers():
    h = etoro_headers(**_keys())
    assert h["x-api-key"] == CFG["api_key"] and h["x-user-key"] == CFG["user_key"]
    assert uuid.UUID(h["x-request-id"])
    assert etoro_headers(**_keys())["x-request-id"] != h["x-request-id"]
    assert etoro_headers(**_keys(), request_id="r-1")["x-request-id"] == "r-1"


async def test_search_instrument_picks_exact_match(plain, mock):
    route = mock.get(SEARCH).mock(return_value=httpx.Response(200, json={"items": [
        _search_item("AAPL.RTH", 9999), _search_item("AAPL", 1001)]}))
    hit = await search_instrument(plain, etoro_headers(**_keys()), "AAPL")
    assert hit is not None and hit["instrumentId"] == 1001
    assert route.calls[0].request.url.params["internalSymbolFull"] == "AAPL"
    route.mock(return_value=httpx.Response(200, json={"items": [_search_item("AAPL.RTH")]}))
    assert await search_instrument(plain, etoro_headers(**_keys()), "AAPL") is None


def test_instrument_cache_per_scope():
    demo, real, data = (instrument_cache(s) for s in ("demo", "real", "data"))
    assert len({id(demo), id(real), id(data)}) == 3
    assert instrument_cache("demo") is demo
    with pytest.raises(ValueError):
        instrument_cache("other")


async def test_symbol_mapping_override_and_crypto(adapter, fake, sent):
    assert adapter.etoro_symbol(_inst("SAP.DE", AssetClass.eu_equity, "EUR",
                                      {"etoro": "SAP.DE"})) == "SAP.DE"
    assert adapter.etoro_symbol(_inst("VOD.L", AssetClass.eu_equity, "GBP",
                                      {"etoro": "VOD.L", "yahoo": "VOD.L"})) == "VOD.L"
    assert adapter.etoro_symbol(_inst("BTC-USD", AssetClass.crypto)) == "BTC"
    assert adapter.etoro_symbol(_inst("AAPL")) == "AAPL"
    assert adapter.etoro_symbol(_inst("MSFT", provider_symbols={"etoro": "MSFT"})) == "MSFT"
    order = _order(symbol="BTC-USD", qty=Decimal("0.01"))
    bo = await adapter.place_order(order, _inst("BTC-USD", AssetClass.crypto))
    assert bo.status == "accepted"
    search = next(r for r in sent if r.url.path.endswith("/search"))
    assert search.url.params["internalSymbolFull"] == "BTC"
    assert _bodies(sent)[0]["instrumentId"] == 100000
    assert await adapter.get_positions() == {"BTC-USD": Decimal("0.01")}


@pytest.mark.parametrize(
    ("items", "eligibility"),
    [
        ([], None),  # not found
        ([_search_item("AAPL", isBuyEnabled=False)], _eligibility()),
        ([_search_item("AAPL", isDelisted=True)], _eligibility()),
        ([_search_item("AAPL")], _eligibility(allow_open=False)),
        ([_search_item("AAPL")], _eligibility(configs=[
            {"settlementType": "cfd", "direction": "buy", "leverageValues": [1, 2]},
            {"settlementType": "real", "direction": "sell", "leverageValues": [1]},
            {"settlementType": "real", "direction": "buy", "leverageValues": [2]}])),
        ([_search_item("AAPL")], {"instruments": []}),
    ],
)
async def test_ineligible_instrument_not_routable(items, eligibility, plain, mock):
    mock.get(SEARCH).mock(return_value=httpx.Response(200, json={"items": items}))
    if eligibility is not None:
        mock.post(ELIGIBILITY).mock(return_value=httpx.Response(200, json=eligibility))
    a = EtoroDemoAdapter(dict(CFG), plain)
    assert a.routable(_order(), _inst()) is True  # optimistic before prepare (no network)
    await a.prepare([_inst()])
    assert a.supports(_order(), _inst()) is False
    assert a.routable(_order(), _inst()) is False
    assert a.unsupported_reason(_order(), _inst()) == NOT_TRADABLE_MESSAGE
    assert NOT_TRADABLE_MESSAGE == "strumento non negoziabile senza leva su questo broker"
    bo = await a.place_order(_order(), _inst())  # cached: no new request, no POST
    assert (bo.status, bo.message) == ("rejected", NOT_TRADABLE_MESSAGE)


async def test_eligibility_request_body(adapter, fake, sent):
    await adapter.prepare([_inst("AAPL"), _inst("MSFT")])
    elig = [r for r in sent if r.url.path.endswith("/eligibility")]
    assert len(elig) == 1
    assert json.loads(elig[0].content) == {"instrumentIds": [1001, 1002], "currency": "USD"}
    assert elig[0].url.path == A.INFO_V2 + "/eligibility"
    calls = len(fake.calls)
    await adapter.prepare([_inst("AAPL"), _inst("MSFT")])  # cached for 24 h
    assert len(fake.calls) == calls


@pytest.mark.parametrize("status", [400, 422])
async def test_open_rejection_without_body_message_has_one(status, plain, mock):
    mock.get(SEARCH).mock(return_value=httpx.Response(200, json={"items": [_search_item()]}))
    mock.post(ELIGIBILITY).mock(return_value=httpx.Response(200, json=_eligibility()))
    mock.post(ETORO_URL + A.EXEC_V2 + "/orders").mock(
        return_value=httpx.Response(status, content=b"<html>"))
    bo = await EtoroDemoAdapter(dict(CFG), plain).place_order(_order(), _inst())
    assert (bo.status, bo.message) == ("rejected", f"etoro: HTTP {status}")


async def test_close_rejection_without_body_message_has_one(adapter, fake, monkeypatch):
    await _buy(adapter, 1)
    monkeypatch.setattr(fake, "_etoro_close",
                        lambda env, pid, body: httpx.Response(422, json={}))
    bo = await adapter.place_order(_order(side=OrderSide.sell, qty=Decimal(1)), _inst())
    assert (bo.status, bo.message) == ("rejected", "etoro: HTTP 422")


async def test_lookup_without_asset_is_rejected(plain, mock, caplog):
    data = {k: v for k, v in _fixture("etoro_order_lookup.json").items() if k != "asset"}
    mock.get(ETORO_URL + A.INFO_V2 + "/orders:lookup", params={"orderId": "77"}).mock(
        return_value=httpx.Response(200, json=data))
    a = EtoroDemoAdapter(dict(CFG), plain)
    with caplog.at_level("WARNING", logger="smtrading.brokers.etoro"):
        first = await a.get_order("77")
        second = await a.get_order("77")
    assert first.status == second.status == "rejected"
    assert len([r for r in caplog.records if "without asset" in r.getMessage()]) == 1


async def test_lookup_filled_without_executions_stays_accepted(plain, mock):
    data = {**_fixture("etoro_order_lookup.json"), "positionExecutions": []}
    mock.get(ETORO_URL + A.INFO_V2 + "/orders:lookup", params={"orderId": "77"}).mock(
        return_value=httpx.Response(200, json=data))
    bo = await EtoroDemoAdapter(dict(CFG), plain).get_order("77")
    assert (bo.status, bo.filled_qty, bo.avg_fill_price) == ("accepted", Decimal(0), None)


async def test_open_rejected_by_broker_400(plain, mock):
    mock.get(SEARCH).mock(return_value=httpx.Response(200, json={"items": [_search_item()]}))
    mock.post(ELIGIBILITY).mock(return_value=httpx.Response(200, json=_eligibility()))
    mock.post(ETORO_URL + A.EXEC_V2 + "/orders").mock(return_value=httpx.Response(
        400, json={"errorMessage": "Insufficient funds", "errorCode": 1001}))
    bo = await EtoroDemoAdapter(dict(CFG), plain).place_order(_order(), _inst())
    assert (bo.status, bo.message) == ("rejected", "Insufficient funds")


async def test_auth_failure_is_retryable_error_without_keys(plain, mock):
    mock.get(PNL).mock(return_value=httpx.Response(401, json={"errorMessage": "Unauthorized"}))
    with pytest.raises(RuntimeError, match="HTTP 401") as e:
        await EtoroDemoAdapter(dict(CFG), plain).health_check()
    assert CFG["api_key"] not in str(e.value) and CFG["user_key"] not in str(e.value)


def test_other_base_url_forbidden():
    with pytest.raises(SMTError) as e:
        EtoroDemoAdapter({**CFG, "base_url": "https://evil.example.com"}, None)
    assert e.value.code == ErrorCode.FORBIDDEN
    assert "evil" not in e.value.message


def test_socket_guard_blocks_etoro_host():
    host = hostname(ETORO_URL)
    with pytest.raises(RuntimeError, match="tentata connessione a un host live nei test"):
        socket.getaddrinfo(host, 443)
    with pytest.raises(RuntimeError, match="tentata connessione a un host live nei test"):
        socket.create_connection((host.upper(), 443))
