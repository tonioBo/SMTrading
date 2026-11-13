import json
import uuid
from decimal import Decimal

import httpx
import pytest
import respx

from smtrading.brokers.alpaca_paper import _STATUS, PAPER_URL, AlpacaPaperAdapter
from smtrading.brokers.service import (
    create_link,
    get_adapter,
    link_portfolio,
    list_links,
    update_link,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import Order, OrderSide, OrderType, TimeInForce
from smtrading.instruments.models import AssetClass, Instrument

CFG = {"key_id": "KID-SECRET", "secret_key": "SEC-SECRET"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


def _inst(symbol="AAPL", ac=AssetClass.us_equity):
    return Instrument(symbol=symbol, name=symbol, asset_class=ac, market="X", currency="USD")


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
    async with httpx.AsyncClient() as c:
        yield c


ORDER_JSON = {
    "id": "b1",
    "status": "partially_filled",
    "filled_qty": "2.5",
    "filled_avg_price": "190.123456789",
}


@respx.mock
async def test_market_payload(http):
    route = respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json=ORDER_JSON)
    )
    o = _order()
    res = await AlpacaPaperAdapter(CFG, http).place_order(o, _inst())
    req = route.calls[0].request
    assert json.loads(req.content) == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": str(o.id),
    }
    assert req.headers["APCA-API-KEY-ID"] == "KID-SECRET"
    assert res.status == "partially_filled"
    assert res.filled_qty == Decimal("2.5")
    assert res.avg_fill_price == Decimal("190.123456789")


@respx.mock
async def test_limit_and_crypto_payload(http):
    route = respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json={"id": "b2", "status": "new", "filled_qty": "0"})
    )
    o = _order(
        symbol="BTC-USD", type=OrderType.limit, limit_price=Decimal("60000.50"), qty=Decimal("0.01")
    )
    res = await AlpacaPaperAdapter(CFG, http).place_order(o, _inst("BTC-USD", AssetClass.crypto))
    body = json.loads(route.calls[0].request.content)
    assert body["symbol"] == "BTC/USD"
    assert body["time_in_force"] == "gtc"
    assert body["limit_price"] == "60000.5"
    assert body["qty"] == "0.01"
    assert res.status == "accepted"
    assert res.avg_fill_price is None


@respx.mock
async def test_trailing_payload(http):
    route = respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json={"id": "b3", "status": "accepted"})
    )
    a = AlpacaPaperAdapter(CFG, http)
    await a.place_order(
        _order(side=OrderSide.sell, type=OrderType.trailing_stop, trail_pct=Decimal("2.5")),
        _inst(),
    )
    await a.place_order(
        _order(side=OrderSide.sell, type=OrderType.trailing_stop, trail_amount=Decimal(3)),
        _inst(),
    )
    b1, b2 = (json.loads(c.request.content) for c in route.calls)
    assert b1["type"] == "trailing_stop" and b1["trail_percent"] == "2.5"
    assert "trail_price" not in b1
    assert b2["trail_price"] == "3"


@pytest.mark.parametrize(
    ("raw", "expected"),
    list(_STATUS.items()),
)
@respx.mock
async def test_status_conversion(http, raw, expected):
    respx.get(f"{PAPER_URL}/v2/orders/x").mock(
        return_value=httpx.Response(200, json={"id": "x", "status": raw})
    )
    assert (await AlpacaPaperAdapter(CFG, http).get_order("x")).status == expected


@respx.mock
async def test_rejection_is_not_an_exception(http):
    respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(422, json={"message": "insufficient buying power"})
    )
    res = await AlpacaPaperAdapter(CFG, http).place_order(_order(), _inst())
    assert res.status == "rejected"
    assert res.message == "insufficient buying power"


@respx.mock
async def test_5xx_raises_without_leaking_keys(http):
    respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )
    with pytest.raises(RuntimeError) as e:
        await AlpacaPaperAdapter(CFG, http).place_order(_order(), _inst())
    assert str(e.value) == "alpaca_paper: HTTP 503: unavailable"


@respx.mock
async def test_network_error_does_not_leak(http):
    respx.get(f"{PAPER_URL}/v2/positions").mock(side_effect=httpx.ConnectError("boom KID-SECRET"))
    with pytest.raises(RuntimeError) as e:
        await AlpacaPaperAdapter(CFG, http).get_positions()
    assert "KID-SECRET" not in str(e.value)


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "http://localhost:1"])
def test_non_paper_url_forbidden(url):
    with pytest.raises(SMTError) as e:
        AlpacaPaperAdapter({**CFG, "base_url": url}, None)
    assert e.value.code == ErrorCode.FORBIDDEN


@respx.mock
async def test_positions_canonical_symbols(http):
    respx.get(f"{PAPER_URL}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "qty": "5", "side": "long", "asset_class": "us_equity"},
                {"symbol": "BTCUSD", "qty": "0.5", "side": "long", "asset_class": "crypto"},
                {"symbol": "TSLA", "qty": "3", "side": "short", "asset_class": "us_equity"},
            ],
        )
    )
    assert await AlpacaPaperAdapter(CFG, http).get_positions() == {
        "AAPL": Decimal(5),
        "BTC-USD": Decimal("0.5"),
        "TSLA": Decimal(-3),
    }


@respx.mock
async def test_cancel_and_health(http):
    d = respx.delete(f"{PAPER_URL}/v2/orders/x").mock(return_value=httpx.Response(204))
    respx.get(f"{PAPER_URL}/v2/account").mock(return_value=httpx.Response(200, json={}))
    a = AlpacaPaperAdapter(CFG, http)
    await a.cancel_order("x")
    await a.health_check()
    assert d.called


def test_supports():
    a = AlpacaPaperAdapter(CFG, None)
    assert a.supports(_order(), _inst())
    assert not a.supports(_order(), _inst("EUR", AssetClass.forex))
    assert not a.supports(_order(type=OrderType.trailing_stop), _inst("BTC-USD", AssetClass.crypto))
    assert not a.supports(_order(type=OrderType.limit, qty=None, notional=Decimal(5)), _inst())


async def test_service_links(db_session, portfolio, http):
    link = await create_link(db_session, "paper1", "alpaca_paper", CFG)
    assert link.config_encrypted and b"SEC-SECRET" not in link.config_encrypted
    assert [lk.name for lk in await list_links(db_session)] == ["paper1"]
    adapter = await get_adapter(db_session, link.id, http)
    assert isinstance(adapter, AlpacaPaperAdapter)
    await update_link(db_session, link.id, enabled=False)
    assert link.enabled is False
    pf = await link_portfolio(db_session, portfolio.id, link.id)
    assert pf.broker_link_id == link.id
    assert (await link_portfolio(db_session, portfolio.id, None)).broker_link_id is None
    with pytest.raises(SMTError) as e:
        await create_link(db_session, "bad", "alpaca_paper", {**CFG, "base_url": "https://x.io"})
    assert e.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(SMTError):
        await create_link(db_session, "bad2", "nope", {})


@respx.mock
async def test_duplicate_client_order_id_returns_existing(http):
    o = _order()
    respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(422, json={"message": "client_order_id must be unique"})
    )
    get = respx.get(f"{PAPER_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json={"id": "existing", "status": "new"})
    )
    res = await AlpacaPaperAdapter(CFG, http).place_order(o, _inst())
    assert res.status == "accepted"
    assert res.broker_order_id == "existing"
    assert get.calls[0].request.url.params["client_order_id"] == str(o.id)


@respx.mock
async def test_redirect_not_followed(http):
    respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(
            307, headers={"location": "https://api.alpaca.markets/v2/orders"}
        )
    )
    live = respx.post("https://api.alpaca.markets/v2/orders").mock(return_value=httpx.Response(200))
    with pytest.raises(RuntimeError):
        await AlpacaPaperAdapter(CFG, http).place_order(_order(), _inst())
    assert not live.called


@pytest.mark.parametrize(
    ("msg", "rejected"),
    [("forbidden.", False), ("Unauthorized access key", False), ("account is blocked", True)],
)
@respx.mock
async def test_403_auth_vs_rejection(http, msg, rejected):
    respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(403, json={"message": msg})
    )
    a = AlpacaPaperAdapter(CFG, http)
    if rejected:
        assert (await a.place_order(_order(), _inst())).status == "rejected"
    else:
        with pytest.raises(RuntimeError, match="credenziali non valide"):
            await a.place_order(_order(), _inst())


@respx.mock
async def test_raw_is_json_safe(http):
    respx.get(f"{PAPER_URL}/v2/orders/x").mock(
        return_value=httpx.Response(200, json={"id": "x", "status": "new", "limit_price": 1.5})
    )
    raw = (await AlpacaPaperAdapter(CFG, http).get_order("x")).raw
    assert json.loads(json.dumps(raw)) == raw
    assert raw["limit_price"] == "1.5"


@respx.mock
async def test_find_order_by_client_reference(http):
    o = _order()
    o.broker_attempt = 2
    route = respx.get(f"{PAPER_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json={"id": "x1", "status": "new", "filled_qty": "0"})
    )
    found = await AlpacaPaperAdapter(CFG, http).find_order(o, _inst(), None)
    assert found is not None and found.broker_order_id == "x1"
    assert route.calls[0].request.url.params["client_order_id"] == f"{o.id}-2"
    route.mock(return_value=httpx.Response(404, json={"message": "not found"}))
    assert await AlpacaPaperAdapter(CFG, http).find_order(o, _inst(), None) is None
