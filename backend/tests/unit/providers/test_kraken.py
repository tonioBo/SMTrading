from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.kraken import KrakenProvider
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
