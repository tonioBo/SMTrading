from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.coingecko import CoinGeckoProvider
from smtrading.marketdata.registry import PROVIDERS

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://api.coingecko.com/api/v3"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield CoinGeckoProvider({}, http)


def test_registered():
    assert PROVIDERS["coingecko"] is CoinGeckoProvider


def test_map_symbol(provider):
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "bitcoin:usd"
    assert provider.map_symbol("USDC-EUR", AssetClass.crypto, None) == "usd-coin:eur"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, "wrapped-bitcoin") == "wrapped-bitcoin"
    for bad in ("FOO-USD", "BTC", "AAPL"):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(bad, AssetClass.crypto, None)
    with pytest.raises(ProviderUnsupported):
        provider.map_symbol("BTC-USD", AssetClass.us_equity, None)


@respx.mock
async def test_quotes_decimal_and_ts(provider):
    route = respx.get(f"{API}/simple/price").respond(
        200, text=fixture("coingecko_simple_price.json")
    )
    quotes = await provider.get_quotes([("BTC-USD", "bitcoin:usd"), ("ETH-USD", "ethereum")])
    params = route.calls.last.request.url.params
    assert params["ids"] == "bitcoin,ethereum"
    assert params["vs_currencies"] == "usd"
    assert params["include_last_updated_at"] == "true"
    btc, eth = quotes["BTC-USD"], quotes["ETH-USD"]
    assert btc.last == Decimal(71451) and eth.last == Decimal("2013.99")
    assert isinstance(eth.last, Decimal)
    assert btc.ts == datetime.fromtimestamp(1794521430, UTC)
    assert btc.bid is None and btc.ask is None and btc.provider == "coingecko"


@respx.mock
async def test_quotes_missing_fields_skipped(provider):
    respx.get(f"{API}/simple/price").respond(200, json={"bitcoin": {}})
    assert await provider.get_quotes([("BTC-USD", "bitcoin:usd"), ("ETH-USD", "ethereum")]) == {}


@respx.mock
async def test_api_key_header():
    respx.get(f"{API}/simple/price").respond(200, json={})
    async with httpx.AsyncClient() as http:
        keyed = CoinGeckoProvider({"api_key": "k"}, http)
        await keyed.get_quotes([("BTC-USD", "bitcoin:usd")])
        assert respx.calls.last.request.headers["x-cg-demo-api-key"] == "k"
        respx.calls.reset()
        await CoinGeckoProvider({}, http).get_quotes([("BTC-USD", "bitcoin:usd")])
        assert "x-cg-demo-api-key" not in respx.calls.last.request.headers


@respx.mock
async def test_search(provider):
    respx.get(f"{API}/search", params={"query": "bitcoin"}).respond(
        200, text=fixture("coingecko_search.json")
    )
    hits = await provider.search("bitcoin", 3)
    assert len(hits) == 3
    assert hits[0].symbol == "BTC-USD" and hits[0].name == "Bitcoin"
    assert hits[0].asset_class == AssetClass.crypto and hits[0].provider == "coingecko"


@respx.mock
async def test_fundamentals(provider):
    respx.get(f"{API}/coins/bitcoin").respond(200, text=fixture("coingecko_coin_bitcoin.json"))
    f = await provider.get_fundamentals("BTC-USD", "bitcoin:usd")
    assert f.name == "Bitcoin" and f.currency == "USD"
    assert f.market_cap == Decimal(1436838893464)
    assert f.high_52w == Decimal(74125) and f.low_52w == Decimal(71238)
    assert f.extra["ath"] == "126080" and f.extra["atl"] == "67.81"
    assert f.pe is None and f.sector is None


@respx.mock
async def test_fundamentals_missing_fields_none(provider):
    respx.get(f"{API}/coins/bitcoin").respond(200, json={"name": "Bitcoin"})
    f = await provider.get_fundamentals("BTC-USD", "bitcoin")
    assert f.market_cap is None and f.high_52w is None and f.low_52w is None


@respx.mock
async def test_http_429_raises(provider):
    respx.get(f"{API}/simple/price").respond(429)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_quotes([("BTC-USD", "bitcoin:usd")])
