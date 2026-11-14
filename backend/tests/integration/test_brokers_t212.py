import base64
import json
import uuid
from decimal import Decimal

import httpx
import pytest
import respx

from smtrading.brokers import t212_demo
from smtrading.brokers.live.t212_live import T212LiveAdapter
from smtrading.brokers.t212_demo import DEMO_URL, T212DemoAdapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

CFG = {"api_key": "KEY-SECRET", "api_secret": "SEC-SECRET"}
API = f"{DEMO_URL}/api/v0/equity"
INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "SAPd_EQ", "shortName": "SAP", "type": "STOCK", "currencyCode": "EUR"},
    {"ticker": "VOD_EQ", "shortName": "VOD", "type": "STOCK", "currencyCode": "GBX"},
    {"ticker": "DUPa_EQ", "shortName": "DUP", "type": "STOCK", "currencyCode": "EUR"},
    {"ticker": "DUPb_EQ", "shortName": "DUP", "type": "STOCK", "currencyCode": "EUR"},
]


@pytest.fixture(autouse=True)
def _reset_cache():
    t212_demo._cache.update(at=0.0, items=None, failed_at=None)
    t212_demo._reverse.clear()
    yield
    t212_demo._cache.update(at=0.0, items=None, failed_at=None)
    t212_demo._reverse.clear()


def _inst(symbol="AAPL", ac=AssetClass.us_equity, cur="USD"):
    return Instrument(symbol=symbol, name=symbol, asset_class=ac, market="X", currency=cur)


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
async def http():
    async with httpx.AsyncClient(follow_redirects=True) as c:
        yield c


def _mock_instruments():
    return respx.get(f"{API}/metadata/instruments").mock(
        return_value=httpx.Response(200, json=INSTRUMENTS)
    )


@respx.mock
async def test_market_buy_and_sell_sign(http):
    _mock_instruments()
    route = respx.post(f"{API}/orders/market").mock(
        return_value=httpx.Response(200, json={"id": 42, "status": "NEW", "filledQuantity": 0})
    )
    a = T212DemoAdapter(CFG, http)
    res = await a.place_order(_order(), _inst())
    await a.place_order(_order(side=OrderSide.sell, qty=Decimal("0.5")), _inst())
    r1, r2 = route.calls[0].request, route.calls[1].request
    assert json.loads(r1.content) == {"ticker": "AAPL_US_EQ", "quantity": 10}
    assert json.loads(r2.content) == {"ticker": "AAPL_US_EQ", "quantity": -0.5}
    expected = base64.b64encode(b"KEY-SECRET:SEC-SECRET").decode()
    assert r1.headers["Authorization"] == f"Basic {expected}"
    assert res.broker_order_id == "42"
    assert res.status == "accepted"


@respx.mock
async def test_limit_stop_stoplimit_payloads_are_numbers(http):
    _mock_instruments()
    rl = respx.post(f"{API}/orders/limit").mock(
        return_value=httpx.Response(200, json={"id": 1, "status": "NEW"})
    )
    rs = respx.post(f"{API}/orders/stop").mock(
        return_value=httpx.Response(200, json={"id": 2, "status": "NEW"})
    )
    rsl = respx.post(f"{API}/orders/stop_limit").mock(
        return_value=httpx.Response(200, json={"id": 3, "status": "NEW"})
    )
    a = T212DemoAdapter(CFG, http)
    await a.place_order(
        _order(type=OrderType.limit, limit_price=Decimal("190.123456789"), tif=TimeInForce.gtc),
        _inst(),
    )
    await a.place_order(
        _order(type=OrderType.stop, side=OrderSide.sell, stop_price=Decimal("180.5")), _inst()
    )
    await a.place_order(
        _order(type=OrderType.stop_limit, stop_price=Decimal(180), limit_price=Decimal("181.25")),
        _inst(),
    )
    raw_limit = rl.calls[0].request.content.decode()
    assert '"limitPrice": 190.123456789' in raw_limit  # exact numeric literal, not a string
    assert json.loads(raw_limit) == {
        "ticker": "AAPL_US_EQ",
        "quantity": 10,
        "timeValidity": "GOOD_TILL_CANCEL",
        "limitPrice": 190.123456789,
    }
    assert json.loads(rs.calls[0].request.content) == {
        "ticker": "AAPL_US_EQ",
        "quantity": -10,
        "timeValidity": "DAY",
        "stopPrice": 180.5,
    }
    assert json.loads(rsl.calls[0].request.content) == {
        "ticker": "AAPL_US_EQ",
        "quantity": 10,
        "timeValidity": "DAY",
        "stopPrice": 180,
        "limitPrice": 181.25,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NEW", "accepted"),
        ("CONFIRMED", "accepted"),
        ("CANCELLING", "accepted"),
        ("PARTIALLY_FILLED", "partially_filled"),
        ("FILLED", "filled"),
        ("CANCELLED", "canceled"),
        ("REJECTED", "rejected"),
    ],
)
@respx.mock
async def test_status_conversion(http, raw, expected):
    respx.get(f"{API}/orders/7").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "status": raw, "filledQuantity": 1.5, "limitPrice": 1.25}
        )
    )
    res = await T212DemoAdapter(CFG, http).get_order("7")
    assert res.status == expected
    assert res.filled_qty == Decimal("1.5")
    assert res.raw["limitPrice"] == "1.25"  # JSON-safe: decimals as strings
    json.dumps(res.raw)


@respx.mock
async def test_get_order_falls_back_to_history_for_fill_price(http):
    respx.get(f"{API}/orders/9").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{API}/history/orders", params={"limit": "50"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"order": {"id": 1, "status": "FILLED"}, "fill": {"price": 1}},
                    {
                        "order": {"id": 9, "status": "FILLED", "filledQuantity": 3},
                        "fill": {"price": 101.5},
                    },
                ],
                "nextPagePath": None,
            },
        )
    )
    res = await T212DemoAdapter(CFG, http).get_order("9")
    assert res.status == "filled"
    assert res.filled_qty == Decimal(3)
    assert res.avg_fill_price == Decimal("101.5")


@respx.mock
async def test_rejection_is_not_an_exception(http):
    _mock_instruments()
    respx.post(f"{API}/orders/market").mock(
        return_value=httpx.Response(400, json={"message": "insufficient funds"})
    )
    res = await T212DemoAdapter(CFG, http).place_order(_order(), _inst())
    assert res.status == "rejected"
    assert res.message == "insufficient funds"


@pytest.mark.parametrize("code", [401, 403])
@respx.mock
async def test_auth_errors_raise(http, code):
    _mock_instruments()
    respx.post(f"{API}/orders/market").mock(return_value=httpx.Response(code, json={}))
    with pytest.raises(RuntimeError, match="credenziali non valide"):
        await T212DemoAdapter(CFG, http).place_order(_order(), _inst())


@respx.mock
async def test_5xx_and_network_errors_do_not_leak(http):
    _mock_instruments()
    respx.post(f"{API}/orders/market").mock(
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )
    with pytest.raises(RuntimeError) as e:
        await T212DemoAdapter(CFG, http).place_order(_order(), _inst())
    assert str(e.value) == "t212_demo: HTTP 503: unavailable"
    respx.get(f"{API}/positions").mock(side_effect=httpx.ConnectError("boom KEY-SECRET"))
    with pytest.raises(RuntimeError) as e2:
        await T212DemoAdapter(CFG, http).get_positions()
    assert "KEY-SECRET" not in str(e2.value)


@respx.mock
async def test_redirect_to_other_host_not_followed(http):
    _mock_instruments()
    evil = respx.get(f"{T212LiveAdapter.BASE_URL}/api/v0/equity/positions").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{API}/positions").mock(
        return_value=httpx.Response(
            302, headers={"location": f"{T212LiveAdapter.BASE_URL}/api/v0/equity/positions"}
        )
    )
    with pytest.raises(RuntimeError, match="redirect"):
        await T212DemoAdapter(CFG, http).get_positions()
    assert not evil.called


@pytest.mark.parametrize("url", [T212LiveAdapter.BASE_URL, "http://localhost:1"])
def test_non_demo_url_forbidden(url):
    with pytest.raises(SMTError) as e:
        T212DemoAdapter({**CFG, "base_url": url}, None)
    assert e.value.code == ErrorCode.FORBIDDEN


@respx.mock
async def test_positions_cancel_health(http):
    _mock_instruments()
    respx.get(f"{API}/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"instrument": {"ticker": "AAPL_US_EQ"}, "quantity": 5.5},
                {"instrument": {"ticker": "SAPd_EQ"}, "quantity": 2},
            ],
        )
    )
    d = respx.delete(f"{API}/orders/x").mock(return_value=httpx.Response(200))
    a = T212DemoAdapter(CFG, http)
    # never a guessed symbol: an unresolved EU ticker stays raw until prepare() maps it
    assert await a.get_positions() == {"AAPL": Decimal("5.5"), "SAPd_EQ": Decimal(2)}
    await a.prepare([_inst("SAP.DE", AssetClass.eu_equity, "EUR")])
    assert await a.get_positions() == {"AAPL": Decimal("5.5"), "SAP.DE": Decimal(2)}
    await a.cancel_order("x")
    await a.health_check()
    assert d.called


@respx.mock
async def test_eu_ticker_mapping_and_supports(http):
    a = T212DemoAdapter(CFG, http)
    eu = _inst("SAP.DE", AssetClass.eu_equity, "EUR")
    # no metadata yet: EU instruments are unsupported (simulated only)
    assert not a.supports(_order(symbol="SAP.DE"), eu)
    meta = _mock_instruments()
    route = respx.post(f"{API}/orders/market").mock(
        return_value=httpx.Response(200, json={"id": 5, "status": "NEW"})
    )
    await a.place_order(_order(symbol="SAP.DE"), eu)
    await a.place_order(_order(symbol="SAP.DE"), eu)
    assert json.loads(route.calls[0].request.content)["ticker"] == "SAPd_EQ"
    assert meta.call_count == 1  # cached
    assert a.supports(_order(symbol="SAP.DE"), eu)
    assert not a.supports(_order(symbol="XXX.DE"), _inst("XXX.DE", AssetClass.eu_equity, "EUR"))
    assert not a.supports(_order(symbol="DUP.DE"), _inst("DUP.DE", AssetClass.eu_equity, "EUR"))
    assert not a.supports(_order(symbol="ZZZ"), _inst("ZZZ"))  # US ticker absent from metadata


def test_supports_types_and_assets():
    a = T212DemoAdapter(CFG, None)
    assert a.supports(_order(), _inst())
    assert not a.supports(_order(type=OrderType.trailing_stop), _inst())
    assert not a.supports(_order(), _inst("BTC-USD", AssetClass.crypto))
    assert not a.supports(_order(qty=None, notional=Decimal(5)), _inst())


@respx.mock
async def test_non_json_or_idless_200_bodies_raise(http):
    _mock_instruments()
    route = respx.post(f"{API}/orders/market")
    a = T212DemoAdapter(CFG, http)
    route.mock(return_value=httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(RuntimeError, match="not JSON"):
        await a.place_order(_order(), _inst())
    route.mock(return_value=httpx.Response(200, json={"status": "NEW"}))
    with pytest.raises(RuntimeError, match="without an id"):
        await a.place_order(_order(), _inst())
    respx.get(f"{API}/positions").mock(return_value=httpx.Response(200, json={"x": 1}))
    with pytest.raises(RuntimeError):
        await a.get_positions()


@respx.mock
async def test_instrument_refresh_backs_off_after_failure(http):
    meta = respx.get(f"{API}/metadata/instruments").mock(
        return_value=httpx.Response(503, json={})
    )
    a = T212DemoAdapter(CFG, http)
    with pytest.raises(RuntimeError):
        await a.prepare()
    with pytest.raises(RuntimeError, match="backing off"):
        await a.prepare()
    assert meta.call_count == 1


@respx.mock
async def test_concurrent_refreshes_share_one_request(http):
    import asyncio

    meta = _mock_instruments()
    a = T212DemoAdapter(CFG, http)
    await asyncio.gather(*(a.prepare() for _ in range(5)))
    assert meta.call_count == 1


def test_routable_defers_eu_resolution_without_metadata():
    a = T212DemoAdapter(CFG, None)
    eu = _inst("SAP.DE", AssetClass.eu_equity, "EUR")
    assert not a.supports(_order(symbol="SAP.DE"), eu)
    assert a.routable(_order(symbol="SAP.DE"), eu)
    assert not a.routable(_order(type=OrderType.trailing_stop), _inst())
    assert not T212DemoAdapter.idempotent_place


@respx.mock
async def test_find_order_matches_ticker_side_qty_and_time(http):
    from datetime import UTC, datetime

    _mock_instruments()
    since = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    respx.get(f"{API}/orders").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "ticker": "AAPL_US_EQ", "quantity": 10, "side": "BUY",
             "createdAt": "2026-11-13T09:00:00Z", "status": "NEW"},  # too old
            {"id": 2, "ticker": "AAPL_US_EQ", "quantity": 7, "side": "BUY",
             "createdAt": "2026-11-13T10:00:05Z", "status": "NEW"},  # other qty
            {"id": 3, "ticker": "AAPL_US_EQ", "quantity": 10, "side": "BUY",
             "createdAt": "2026-11-13T10:00:02Z", "status": "NEW", "filledQuantity": 0},
        ])
    )
    respx.get(f"{API}/history/orders", params={"limit": "50"}).mock(
        return_value=httpx.Response(200, json={"items": [], "nextPagePath": None})
    )
    a = T212DemoAdapter(CFG, http)
    found = await a.find_order(_order(), _inst(), since)
    assert found is not None and found.broker_order_id == "3"
    # C1: a broker order already linked to another order is never matched again
    assert await a.find_order(_order(), _inst(), since, frozenset({"3"})) is None
    assert await a.find_order(_order(side=OrderSide.sell), _inst(), since) is None


@respx.mock
async def test_cancel_4xx_is_terminal_5xx_raises(http):
    a = T212DemoAdapter(CFG, http)
    route = respx.delete(f"{API}/orders/x")
    route.mock(return_value=httpx.Response(400, json={"code": "NotCancellable"}))
    await a.cancel_order("x")  # BrokerSync reads it back
    route.mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(RuntimeError):
        await a.cancel_order("x")
