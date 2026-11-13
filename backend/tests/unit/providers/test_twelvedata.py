import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
import websockets

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.twelvedata import TwelveDataProvider, parse_twelvedata_ws
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://api.twelvedata.com"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield TwelveDataProvider({"api_key": "k"}, http)


def test_registered_and_metadata():
    assert PROVIDERS["twelvedata"] is TwelveDataProvider
    assert TwelveDataProvider.capabilities == {
        Capability.quote, Capability.bars, Capability.stream, Capability.fx}
    assert TwelveDataProvider.asset_classes == {
        AssetClass.eu_equity, AssetClass.forex, AssetClass.us_equity}
    assert TwelveDataProvider.requires_key is True
    assert TwelveDataProvider.default_rate_limit_per_min == 8


def test_map_symbol(provider):
    assert provider.map_symbol("EURUSD", AssetClass.forex, None) == "EUR/USD"
    assert provider.map_symbol("ENI.MI", AssetClass.eu_equity, None) == "ENI:MTA"
    assert provider.map_symbol("SAP.DE", AssetClass.eu_equity, None) == "SAP:XETR"
    assert provider.map_symbol("AAPL", AssetClass.us_equity, None) == "AAPL"
    assert provider.map_symbol("AAPL", AssetClass.us_equity, "AAPL:NASDAQ") == "AAPL:NASDAQ"
    for sym, ac in [("EUR", AssetClass.forex), ("ENI.XX", AssetClass.eu_equity),
                    ("ENI", AssetClass.eu_equity), ("BTC-USD", AssetClass.crypto)]:
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(sym, ac, None)


async def test_construction_empty_config_and_no_key_unsupported():
    async with httpx.AsyncClient() as http:
        p = TwelveDataProvider({}, http)
        with pytest.raises(ProviderUnsupported):
            await p.get_quotes([("AAPL", "AAPL")])
        with pytest.raises(ProviderUnsupported):
            await p.health_check()
        with pytest.raises(ProviderUnsupported):
            async for _ in p.stream([("AAPL", "AAPL")]):
                pass


@respx.mock
async def test_single_quote_decimal(provider):
    route = respx.get(f"{API}/quote").respond(200, text=fixture("twelvedata_quote.json"))
    quotes = await provider.get_quotes([("AAPL", "AAPL")])
    params = route.calls.last.request.url.params
    assert params["symbol"] == "AAPL" and params["apikey"] == "k"
    q = quotes["AAPL"]
    assert q.last == Decimal("148.85001") and q.bid is None and q.ask is None
    assert q.ts == datetime.fromtimestamp(1631772000, UTC) and q.provider == "twelvedata"


@respx.mock
async def test_batch_quote_and_exchange_qualified(provider):
    route = respx.get(f"{API}/quote").mock(side_effect=lambda req: httpx.Response(
        200, text=fixture("twelvedata_quote_batch.json") if "," in req.url.params["symbol"]
        else fixture("twelvedata_quote.json")))
    quotes = await provider.get_quotes([("AAPL", "AAPL"), ("EURUSD", "EUR/USD"), ("ENI.MI", "ENI:MTA")])
    assert quotes["EURUSD"].bid == Decimal("1.17498") and quotes["EURUSD"].ask == Decimal("1.17502")
    assert set(quotes) == {"AAPL", "EURUSD", "ENI.MI"}
    assert route.call_count == 2
    qualified = route.calls.last.request.url.params
    assert qualified["symbol"] == "ENI" and qualified["exchange"] == "MTA"


@respx.mock
async def test_error_with_http_200_raises(provider):
    respx.get(f"{API}/quote").respond(200, text=fixture("twelvedata_error.json"))
    with pytest.raises(RuntimeError, match="400.*Invalid"):
        await provider.get_quotes([("AAPL", "AAPL")])


@respx.mock
async def test_error_inside_batch_entry_raises(provider):
    batch = {"AAPL": {"code": 404, "message": "symbol not found", "status": "error"},
             "MSFT": {"symbol": "MSFT", "close": "1"}}
    respx.get(f"{API}/quote").respond(200, json=batch)
    with pytest.raises(RuntimeError, match="not found"):
        await provider.get_quotes([("AAPL", "AAPL"), ("MSFT", "MSFT")])


@respx.mock
async def test_http_error_propagates(provider):
    respx.get(f"{API}/quote").respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_quotes([("AAPL", "AAPL")])


@respx.mock
async def test_bars(provider):
    route = respx.get(f"{API}/time_series").respond(200, text=fixture("twelvedata_time_series.json"))
    start = datetime(2021, 9, 16, 19, 0, tzinfo=UTC)
    end = datetime(2021, 9, 16, 20, 0, tzinfo=UTC)
    bars = await provider.get_bars("AAPL", "AAPL", Timeframe.m1, start, end)
    p = route.calls.last.request.url.params
    assert p["interval"] == "1min" and p["start_date"] == "2021-09-16T19:00:00"
    assert p["end_date"] == "2021-09-16T20:00:00" and p["timezone"] == "UTC" and p["order"] == "asc"
    assert len(bars) == 2
    b = bars[1]
    assert b.ts == datetime(2021, 9, 16, 15, 59, tzinfo=UTC)
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal("148.84000"), Decimal("148.90000"), Decimal("148.80000"),
        Decimal("148.85001"), Decimal(500100))


@respx.mock
async def test_bars_intervals_and_daily(provider):
    daily = {"values": [{"datetime": "2021-09-16", "open": "1", "high": "2", "low": "1", "close": "2",
                         "volume": "5"}], "status": "ok"}
    route = respx.get(f"{API}/time_series").respond(200, json=daily)
    now = datetime.now(UTC)
    for tf, expected in [("5m", "5min"), ("15m", "15min"), ("1h", "1h"), ("1d", "1day"), ("1wk", "1week")]:
        bars = await provider.get_bars("AAPL", "AAPL", Timeframe(tf), now, now)
        assert route.calls.last.request.url.params["interval"] == expected
    assert bars[0].ts == datetime(2021, 9, 16, tzinfo=UTC)


@respx.mock
async def test_fx_rate_decimal(provider):
    route = respx.get(f"{API}/exchange_rate").respond(200, text=fixture("twelvedata_exchange_rate.json"))
    assert await provider.get_fx_rate("EUR", "USD") == Decimal("1.17512")
    assert route.calls.last.request.url.params["symbol"] == "EUR/USD"


@respx.mock
async def test_health_check(provider):
    respx.get(f"{API}/api_usage").respond(200, json={"timestamp": "x", "current_usage": 1})
    await provider.health_check()


def test_parse_ws_price():
    msg = json.loads(fixture("twelvedata_ws_price.json"), parse_float=Decimal)
    [q] = parse_twelvedata_ws(msg, {"EUR/USD": "EURUSD"})
    assert (q.symbol, q.last, q.bid, q.ask) == ("EURUSD", Decimal("1.17512"), Decimal("1.17510"), Decimal("1.17514"))
    assert q.ts == datetime.fromtimestamp(1592249566, UTC)


def test_parse_ws_ignores_noise():
    rev = {"EUR/USD": "EURUSD"}
    assert parse_twelvedata_ws({"event": "heartbeat"}, rev) == []
    assert parse_twelvedata_ws({"event": "subscribe-status", "status": "ok"}, rev) == []
    assert parse_twelvedata_ws({"event": "price", "symbol": "XXX", "price": 1}, rev) == []


async def test_stream_end_to_end_then_connection_error():
    received: list[dict] = []
    status = {"event": "subscribe-status", "status": "ok", "success": [{"symbol": "EUR/USD"}], "fails": []}

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send(json.dumps(status))
        await ws.send(fixture("twelvedata_ws_price.json"))
        await ws.send(json.dumps({"event": "price", "symbol": "ENI", "price": 15.5, "timestamp": 1592249570}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            provider = TwelveDataProvider({"api_key": "k", "ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in provider.stream([("EURUSD", "EUR/USD"), ("ENI.MI", "ENI:MTA")]):
                    quotes.append(q)
    assert received == [{"action": "subscribe", "params": {"symbols": "EUR/USD,ENI:MTA"}}]
    assert [(q.symbol, q.last) for q in quotes] == [("EURUSD", Decimal("1.17512")), ("ENI.MI", Decimal("15.5"))]


async def test_stream_subscribe_rejected():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"event": "subscribe-status", "status": "error", "success": [],
                                  "fails": [{"symbol": "ZZZ"}]}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            provider = TwelveDataProvider({"api_key": "k", "ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError, match="rejected"):
                async for _ in provider.stream([("ZZZ", "ZZZ")]):
                    pass
