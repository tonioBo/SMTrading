import json
from datetime import UTC, datetime
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import respx
import websockets

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.kraken import KrakenProvider, parse_kraken_ws
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://api.kraken.com/0/public"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield KrakenProvider({}, http)


def test_registered_and_metadata():
    assert PROVIDERS["kraken"] is KrakenProvider
    assert KrakenProvider.capabilities == {Capability.quote, Capability.bars, Capability.stream}
    assert KrakenProvider.asset_classes == {AssetClass.crypto}
    assert KrakenProvider.default_rate_limit_per_min == 60


def test_map_symbol(provider):
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "XBTUSD"
    assert provider.map_symbol("ETH-USD", AssetClass.crypto, None) == "ETHUSD"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, "XXBTZEUR") == "XXBTZEUR"
    for bad in ("AAPL", "BTC"):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(bad, AssetClass.crypto, None)
    with pytest.raises(ProviderUnsupported):
        provider.map_symbol("AAPL", AssetClass.us_equity, None)


@respx.mock
async def test_quotes_decimal_and_key_resolution(provider):
    pairs = respx.get(f"{API}/AssetPairs").respond(200, text=fixture("kraken_asset_pairs.json"))
    ticker = respx.get(f"{API}/Ticker").respond(200, text=fixture("kraken_ticker.json"))
    items = [("BTC-USD", "XBTUSD"), ("ETH-USD", "ETHUSD")]
    quotes = await provider.get_quotes(items)
    assert ticker.calls.last.request.url.params["pair"] == "XBTUSD,ETHUSD"
    btc, eth = quotes["BTC-USD"], quotes["ETH-USD"]
    assert (btc.bid, btc.ask, btc.last) == (
        Decimal("71373.80000"),
        Decimal("71373.90000"),
        Decimal("71373.80000"),
    )
    assert eth.last == Decimal("2010.26000") and isinstance(eth.bid, Decimal)
    assert btc.provider == "kraken" and btc.ts.tzinfo is not None
    await provider.get_quotes(items)
    assert pairs.call_count == 1  # altname map cached


@respx.mock
async def test_alias_pair_resolved_once_and_cached(provider):
    full = json.loads(fixture("kraken_asset_pairs.json"))

    def asset_pairs(request):
        wanted = request.url.params["pair"].split(",")
        if wanted == ["BTCUSD"]:  # alias lookup on its own: Kraken answers with the key
            return httpx.Response(200, json={"error": [], "result": {
                "XXBTZUSD": full["result"]["XXBTZUSD"]}})
        return httpx.Response(200, json=full)

    pairs = respx.get(f"{API}/AssetPairs").mock(side_effect=asset_pairs)
    respx.get(f"{API}/Ticker").respond(200, text=fixture("kraken_ticker.json"))
    items = [("BTC-USD", "BTCUSD"), ("ETH-USD", "ETHUSD")]
    quotes = await provider.get_quotes(items)
    assert quotes["BTC-USD"].last == Decimal("71373.80000")
    assert set(quotes) == {"BTC-USD", "ETH-USD"}
    await provider.get_quotes(items)
    assert pairs.call_count == 2  # batch + one alias lookup, then fully cached


@respx.mock
async def test_quotes_error_raises(provider):
    respx.get(f"{API}/AssetPairs").respond(200, json={"error": ["EQuery:Unknown asset pair"]})
    with pytest.raises(RuntimeError, match="Unknown asset pair"):
        await provider.get_quotes([("BTC-USD", "XBTUSD")])


@respx.mock
async def test_http_error_propagates(provider):
    respx.get(f"{API}/AssetPairs").respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_quotes([("BTC-USD", "XBTUSD")])


@respx.mock
async def test_bars(provider):
    route = respx.get(f"{API}/OHLC").respond(200, text=fixture("kraken_ohlc.json"))
    start = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    end = datetime.fromtimestamp(1791936000, UTC)  # inclusive upper bound: drops the last 2 rows
    bars = await provider.get_bars("BTC-USD", "XBTUSD", Timeframe.h1, start, end)
    params = route.calls.last.request.url.params
    assert params["pair"] == "XBTUSD" and params["interval"] == "60"
    assert params["since"] == str(int(start.timestamp()))
    assert len(bars) == 3
    first = bars[0]
    assert first.ts == datetime.fromtimestamp(1791928800, UTC)
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        Decimal("83459.2"),
        Decimal("83469.2"),
        Decimal("83325.5"),
        Decimal("83453.0"),
        Decimal("86.47615670"),
    )


@respx.mock
async def test_bars_intervals(provider):
    route = respx.get(f"{API}/OHLC").respond(200, text=fixture("kraken_ohlc.json"))
    now = datetime.now(UTC)
    for tf, expected in [("1m", 1), ("5m", 5), ("15m", 15), ("1d", 1440), ("1wk", 10080)]:
        await provider.get_bars("BTC-USD", "XBTUSD", Timeframe(tf), now, now)
        assert route.calls.last.request.url.params["interval"] == str(expected)


@respx.mock
async def test_bars_error_raises(provider):
    respx.get(f"{API}/OHLC").respond(200, json={"error": ["EGeneral:Invalid arguments"]})
    now = datetime.now(UTC)
    with pytest.raises(RuntimeError):
        await provider.get_bars("BTC-USD", "XBTUSD", Timeframe.h1, now, now)


@respx.mock
async def test_health_check(provider):
    respx.get(f"{API}/SystemStatus").respond(200, json={"error": [], "result": {"status": "online"}})
    await provider.health_check()


def test_ws_symbol(provider):
    assert provider.ws_symbol("BTC-USD") == "BTC/USD"


def test_parse_kraken_ws_snapshot():
    msg = json.loads(fixture("kraken_ws_ticker.json"), parse_float=Decimal)
    [q] = parse_kraken_ws(msg, {"BTC/USD": "BTC-USD"})
    assert q.symbol == "BTC-USD" and q.provider == "kraken"
    assert (q.bid, q.ask, q.last) == (Decimal("71341.0"), Decimal("71341.1"), Decimal("71341.1"))
    assert isinstance(q.bid, Decimal)
    assert q.ts == datetime(2026, 11, 12, 22, 22, 20, 76908, tzinfo=UTC)


def test_parse_kraken_ws_ignores_noise():
    rev = {"BTC/USD": "BTC-USD"}
    assert parse_kraken_ws({"channel": "heartbeat"}, rev) == []
    assert parse_kraken_ws({"channel": "status", "type": "update", "data": [{}]}, rev) == []
    assert parse_kraken_ws({"method": "subscribe", "success": True}, rev) == []
    unknown = {"channel": "ticker", "type": "update", "data": [{"symbol": "DOGE/USD", "bid": 1}]}
    assert parse_kraken_ws(unknown, rev) == []


def test_parse_kraken_ws_update_without_timestamp_uses_now():
    msg = {"channel": "ticker", "type": "update", "data": [{"symbol": "BTC/USD", "bid": 1, "ask": 2, "last": 3}]}
    [q] = parse_kraken_ws(msg, {"BTC/USD": "BTC-USD"})
    assert abs((datetime.now(UTC) - q.ts).total_seconds()) < 5


async def test_stream_end_to_end_then_connection_error():
    received: list[dict] = []
    snapshot = fixture("kraken_ws_ticker.json")
    update = json.dumps(
        {"channel": "ticker", "type": "update", "data": [
            {"symbol": "ETH/USD", "bid": 2010.1, "ask": 2010.2, "last": 2010.2,
             "timestamp": "2026-11-12T22:23:00.000000Z"}]}
    )

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        await ws.send('{"channel":"heartbeat"}')
        await ws.send(snapshot)
        await ws.send(update)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            provider = KrakenProvider({"ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in provider.stream([("BTC-USD", "XBTUSD"), ("ETH-USD", "ETHUSD")]):
                    quotes.append(q)
    assert received == [
        {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD", "ETH/USD"]}}
    ]
    assert [q.symbol for q in quotes] == ["BTC-USD", "ETH-USD"]
    assert quotes[1].last == Decimal("2010.2")


async def test_stream_subscribe_rejected_raises():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"method": "subscribe", "success": False,
                                  "error": "Currency pair not supported XYZ/USD"}))
        await ws.send(fixture("kraken_ws_ticker.json"))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            provider = KrakenProvider({"ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError, match="not supported"):
                async for q in provider.stream([("BTC-USD", "XBTUSD")]):
                    quotes.append(q)
    assert quotes == []


async def test_stream_handshake_failure_is_connection_error():
    async with httpx.AsyncClient() as http:
        provider = KrakenProvider({"ws_url": "ws://127.0.0.1:1"}, http)
        with pytest.raises(ConnectionError):
            async for _ in provider.stream([("BTC-USD", "XBTUSD")]):
                pass


async def test_stream_http_rejection_is_connection_error():
    async def reject(connection, request):
        return connection.respond(HTTPStatus.FORBIDDEN, "no\n")

    async def handler(ws):  # pragma: no cover - never reached
        pass

    async with websockets.serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            provider = KrakenProvider({"ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError):
                async for _ in provider.stream([("BTC-USD", "XBTUSD")]):
                    pass
