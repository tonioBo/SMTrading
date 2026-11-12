import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.binance import BinanceProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://api.binance.com/api/v3"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield BinanceProvider({}, http)


def test_registered_and_metadata():
    assert PROVIDERS["binance"] is BinanceProvider
    assert BinanceProvider.capabilities == {Capability.quote, Capability.bars, Capability.stream}
    assert BinanceProvider.asset_classes == {AssetClass.crypto}
    assert BinanceProvider.default_rate_limit_per_min == 600


def test_map_symbol(provider):
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "BTCUSDT"
    assert provider.map_symbol("ETH-EUR", AssetClass.crypto, None) == "ETHEUR"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, "BTCFDUSD") == "BTCFDUSD"
    for bad in ("AAPL", "BTC"):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(bad, AssetClass.crypto, None)
    with pytest.raises(ProviderUnsupported):
        provider.map_symbol("AAPL", AssetClass.us_equity, None)


@respx.mock
async def test_quotes(provider):
    book = respx.get(f"{API}/ticker/bookTicker").respond(200, text=fixture("binance_book_ticker.json"))
    price = respx.get(f"{API}/ticker/price").respond(200, text=fixture("binance_price.json"))
    quotes = await provider.get_quotes([("BTC-USD", "BTCUSDT"), ("ETH-USD", "ETHUSDT")])
    for route in (book, price):
        assert json.loads(route.calls.last.request.url.params["symbols"]) == ["BTCUSDT", "ETHUSDT"]
    btc, eth = quotes["BTC-USD"], quotes["ETH-USD"]
    assert (btc.bid, btc.ask, btc.last) == (
        Decimal("71464.06000000"),
        Decimal("71464.07000000"),
        Decimal("71424.85000000"),
    )
    assert isinstance(eth.bid, Decimal) and eth.last == Decimal("2012.23000000")
    assert btc.provider == "binance" and btc.ts.tzinfo is not None


@respx.mock
async def test_geo_block_451_raises(provider):
    respx.get(f"{API}/ticker/bookTicker").respond(451, json={"code": 0, "msg": "restricted location"})
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_quotes([("BTC-USD", "BTCUSDT")])


@respx.mock
async def test_bars(provider):
    route = respx.get(f"{API}/klines").respond(200, text=fixture("binance_klines.json"))
    start = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    bars = await provider.get_bars("BTC-USD", "BTCUSDT", Timeframe.h1, start, end)
    params = route.calls.last.request.url.params
    assert params["symbol"] == "BTCUSDT" and params["interval"] == "1h"
    assert params["startTime"] == str(int(start.timestamp() * 1000))
    assert params["endTime"] == str(int(end.timestamp() * 1000))
    assert len(bars) == 5
    first = bars[0]
    assert first.ts == datetime.fromtimestamp(1794506400, UTC)
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        Decimal("71906.98000000"),
        Decimal("72076.98000000"),
        Decimal("71577.79000000"),
        Decimal("71757.35000000"),
        Decimal("481.50158000"),
    )


@respx.mock
async def test_bars_weekly_interval(provider):
    route = respx.get(f"{API}/klines").respond(200, json=[])
    now = datetime.now(UTC)
    assert await provider.get_bars("BTC-USD", "BTCUSDT", Timeframe.w1, now, now) == []
    assert route.calls.last.request.url.params["interval"] == "1w"


@respx.mock
async def test_health_check(provider):
    respx.get(f"{API}/ping").respond(200, json={})
    await provider.health_check()
