import json
import uuid
from decimal import Decimal

import httpx
import pytest
import respx

from smtrading.brokers.alpaca_paper import _STATUS, PAPER_URL, AlpacaPaperAdapter
from smtrading.brokers.live.alpaca_live import AlpacaLiveAdapter
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

D = Decimal
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


@pytest.mark.parametrize("url", [AlpacaLiveAdapter.BASE_URL, "http://localhost:1"])
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


def test_fractional_rules_shared_by_paper():
    """Alpaca (paper and live): fractional shares never as a trailing stop; a gtc resting
    fractional order is renewed every session (sent as day)."""
    a = AlpacaPaperAdapter(CFG, None)
    inst, half = _inst(), Decimal("0.5")
    assert not a.supports(_order(qty=half, type=OrderType.trailing_stop,
                                 trail_pct=Decimal(1)), inst)
    gtc_stop = _order(qty=half, type=OrderType.stop, stop_price=Decimal(90),
                      tif=TimeInForce.gtc, side=OrderSide.sell)
    assert a.supports(gtc_stop, inst) and a.renewed_daily(gtc_stop, inst)
    for kind in (OrderType.limit, OrderType.stop_limit):
        o = _order(qty=half, type=kind, limit_price=Decimal(1), stop_price=Decimal(1),
                   tif=TimeInForce.gtc)
        assert a.renewed_daily(o, inst)
    # never renewed: whole shares, day orders, market orders, crypto (gtc at Alpaca)
    assert not a.renewed_daily(_order(qty=Decimal(2), type=OrderType.stop,
                                      stop_price=Decimal(9), tif=TimeInForce.gtc), inst)
    assert not a.renewed_daily(_order(qty=half, type=OrderType.stop, stop_price=Decimal(9)),
                               inst)
    assert not a.renewed_daily(_order(qty=half, tif=TimeInForce.gtc), inst)
    btc = _inst("BTC-USD", AssetClass.crypto)
    assert not a.renewed_daily(_order(symbol="BTC-USD", qty=Decimal("0.01"),
                                      type=OrderType.limit, limit_price=Decimal(1),
                                      tif=TimeInForce.gtc), btc)


@respx.mock
@pytest.mark.parametrize("qty,tif,sent", [
    ("0.5", TimeInForce.gtc, "day"), ("0.5", TimeInForce.day, "day"),
    ("2", TimeInForce.gtc, "gtc"), ("2.000", TimeInForce.gtc, "gtc"),
])
async def test_fractional_gtc_is_sent_as_day(http, qty, tif, sent):
    route = respx.post(f"{PAPER_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json={"id": "b9", "status": "new"}))
    o = _order(qty=Decimal(qty), tif=tif, type=OrderType.stop, stop_price=Decimal(90),
               side=OrderSide.sell)
    await AlpacaPaperAdapter(CFG, http).place_order(o, _inst())
    assert json.loads(route.calls[0].request.content)["time_in_force"] == sent


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
            307, headers={"location": f"{AlpacaLiveAdapter.BASE_URL}/v2/orders"}
        )
    )
    live = respx.post(f"{AlpacaLiveAdapter.BASE_URL}/v2/orders").mock(
        return_value=httpx.Response(200)
    )
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
    assert await AlpacaPaperAdapter(CFG, http).find_order(o, _inst(), None, frozenset({"x1"})) is None
    route.mock(return_value=httpx.Response(404, json={"message": "not found"}))
    assert await AlpacaPaperAdapter(CFG, http).find_order(o, _inst(), None) is None


@respx.mock
async def test_get_open_orders_reads_the_open_list(http):
    """Reconciliation: GET only; open quantity = qty − filled; a trailing stop's moving
    level is left out; crypto symbols mapped back."""
    route = respx.get(f"{PAPER_URL}/v2/orders").mock(return_value=httpx.Response(200, json=[
        {"id": "o1", "symbol": "AAPL", "asset_class": "us_equity", "side": "buy",
         "type": "limit", "qty": "10", "filled_qty": "4", "limit_price": "150.5",
         "stop_price": None},
        {"id": "o2", "symbol": "BTC/USD", "asset_class": "crypto", "side": "sell",
         "type": "trailing_stop", "qty": "0.5", "filled_qty": "0", "stop_price": "60000"},
        {"id": "o3", "symbol": "MSFT", "side": "buy", "type": "market", "qty": None,
         "notional": "100"},
    ]))
    orders = await AlpacaPaperAdapter(CFG, http).get_open_orders()
    assert route.calls.last.request.method == "GET"
    assert route.calls.last.request.url.params["status"] == "open"
    assert [(o.broker_order_id, o.symbol, o.side, o.qty, o.limit_price, o.stop_price)
            for o in orders] == [
        ("o1", "AAPL", "buy", Decimal(6), Decimal("150.5"), None),
        ("o2", "BTC-USD", "sell", Decimal("0.5"), None, None),
        ("o3", "MSFT", "buy", None, None, None),
    ]


# -- crypto fee (plan 37): the paper API's answers read on 21/11 (ids kept, keys never) --

BUY_0015 = {
    "id": "308f1731-59d8-4025-a8f2-da305a11abca",
    "client_order_id": "1ed4258c-fe22-487b-9791-5e11a5586364",
    "created_at": "2026-11-21T20:48:13.991954188Z",
    "updated_at": "2026-11-21T20:48:14.03654738Z",
    "submitted_at": "2026-11-21T20:48:13.991954188Z",
    "filled_at": "2026-11-21T20:48:13.994071785Z",
    "asset_class": "crypto", "symbol": "BTC/USD", "notional": None, "qty": "0.0015",
    "filled_qty": "0.0015", "filled_avg_price": "60271.008", "order_class": "",
    "order_type": "limit", "type": "limit", "side": "buy", "position_intent": "buy_to_open",
    "time_in_force": "gtc", "limit_price": "60828.76", "stop_price": None,
    "status": "filled", "legs": None,
}
SELL_00149625 = {
    **BUY_0015, "id": "1fbf5875-1d60-4966-837a-5752911c38c5",
    "submitted_at": "2026-11-21T20:48:38.621689209Z",
    "filled_at": "2026-11-21T20:48:38.623668609Z", "qty": "0.00149625",
    "filled_qty": "0.00149625", "filled_avg_price": "60210.7", "order_type": "market",
    "type": "market", "side": "sell", "position_intent": "sell_to_close",
    "limit_price": None,
}
ACCOUNT_TIER_1 = {"id": "acct", "status": "ACTIVE", "crypto_status": "ACTIVE",
                  "currency": "USD", "cash": "99999.45", "crypto_tier": 1}
BTC_ASSET = {"class": "crypto", "exchange": "CRYPTO", "symbol": "BTC/USD",
             "status": "active", "tradable": True, "fractionable": True,
             "min_order_size": "0.00001709", "min_trade_increment": "0.000000001",
             "price_increment": "0.000000001"}


def _crypto_routes(order: dict) -> None:
    respx.get(f"{PAPER_URL}/v2/orders/{order['id']}").mock(
        return_value=httpx.Response(200, json=order))
    respx.get(f"{PAPER_URL}/v2/account").mock(
        return_value=httpx.Response(200, json=ACCOUNT_TIER_1))
    respx.get(f"{PAPER_URL}/v2/assets/BTC/USD").mock(
        return_value=httpx.Response(200, json=BTC_ASSET))


@respx.mock
async def test_crypto_fill_carries_the_tier_fee_rate(http):
    """No fee anywhere in the order entity: the rate of the account's tier, taker for an
    order filled on arrival or a market order, maker for one that rested."""
    a = AlpacaPaperAdapter(CFG, http)
    _crypto_routes(BUY_0015)
    bo = await a.get_order(BUY_0015["id"])
    assert (bo.filled_qty, bo.fees, bo.fee_rate) == (D("0.0015"), None, D("0.0025"))
    _crypto_routes(SELL_00149625)
    assert (await a.get_order(SELL_00149625["id"])).fee_rate == D("0.0025")
    rested = {**BUY_0015, "id": "r1", "filled_at": "2026-11-21T21:30:00.000000001Z"}
    _crypto_routes(rested)
    assert (await a.get_order("r1")).fee_rate == D("0.0015")
    assert a.fee_dust("BTC-USD", D("0.00149625")) == D("0.00001709")  # min order size
    assert a.fee_dust("BTC-USD", D(1)) == D("0.003")
    assert a.fee_dust("AAPL", D(10)) == 0


@respx.mock
async def test_crypto_order_without_fills_reads_nothing_more(http):
    respx.get(f"{PAPER_URL}/v2/orders/n1").mock(return_value=httpx.Response(
        200, json={**BUY_0015, "id": "n1", "status": "new", "filled_qty": "0",
                   "filled_avg_price": None, "filled_at": None}))
    bo = await AlpacaPaperAdapter(CFG, http).get_order("n1")
    assert bo.fee_rate is None
    assert len(respx.calls) == 1


@respx.mock
async def test_equity_fill_keeps_the_commission_and_no_rate(http):
    respx.get(f"{PAPER_URL}/v2/orders/e1").mock(return_value=httpx.Response(200, json={
        "id": "e1", "status": "filled", "symbol": "AAPL", "asset_class": "us_equity",
        "side": "sell", "type": "market", "filled_qty": "10", "filled_avg_price": "190",
        "commission": "0.03"}))
    bo = await AlpacaPaperAdapter(CFG, http).get_order("e1")
    assert (bo.fees, bo.fee_rate) == (D("0.03"), None)
    assert len(respx.calls) == 1  # no account read


@respx.mock
async def test_asset_balance_reads_the_crypto_position(http):
    a = AlpacaPaperAdapter(CFG, http)
    route = respx.get(f"{PAPER_URL}/v2/positions/BTCUSD").mock(side_effect=[
        httpx.Response(404, json={"code": 40410000, "message": "position does not exist"}),
        httpx.Response(200, json={"symbol": "BTCUSD", "asset_class": "crypto",
                                  "qty": "0.00149625", "qty_available": "0.00149625",
                                  "side": "long"}),
    ])
    btc = _inst("BTC-USD", AssetClass.crypto)
    assert await a.asset_balance("BTC-USD", btc) == (D(0), D(0))
    assert await a.asset_balance("BTC-USD", btc) == (D("0.00149625"), D("0.00149625"))
    assert await a.asset_balance("AAPL", _inst()) is None
    assert route.call_count == 2


def test_crypto_resting_orders_but_buy_limits_are_kept_here(http):
    a = AlpacaPaperAdapter(CFG, http)
    btc, aapl = _inst("BTC-USD", AssetClass.crypto), _inst()

    def o(side, type, **kw):
        return _order(symbol="BTC-USD", side=side, type=type, qty=D("0.001"), **kw)

    stop = o(OrderSide.sell, OrderType.stop, stop_price=D(50000))
    tp = o(OrderSide.sell, OrderType.limit, limit_price=D(70000))
    trail = o(OrderSide.sell, OrderType.trailing_stop, trail_pct=D(2))
    for kept in (stop, tp, trail, o(OrderSide.buy, OrderType.stop, stop_price=D(70000))):
        assert not a.routable(kept, btc) and a.synthetic(kept, btc)
    for sent in (o(OrderSide.buy, OrderType.limit, limit_price=D(50000)),
                 o(OrderSide.buy, OrderType.market), o(OrderSide.sell, OrderType.market)):
        assert a.routable(sent, btc) and not a.synthetic(sent, btc)
    eq_tp = _order(side=OrderSide.sell, type=OrderType.limit, limit_price=D(200))
    eq_stop = _order(side=OrderSide.sell, type=OrderType.stop, stop_price=D(150))
    for sent in (eq_tp, eq_stop):  # stocks unchanged: at the broker
        assert a.routable(sent, aapl) and not a.synthetic(sent, aapl)
