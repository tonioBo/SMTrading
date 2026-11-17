from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.frankfurter import FrankfurterProvider
from smtrading.marketdata.registry import PROVIDERS

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
URL = "https://api.frankfurter.dev/v1/latest"


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield FrankfurterProvider({}, http)


def test_registered():
    assert PROVIDERS["frankfurter"] is FrankfurterProvider


def test_map_symbol(provider):
    assert provider.map_symbol("EURUSD", AssetClass.forex, None) == "EUR/USD"
    assert provider.map_symbol("EURUSD", AssetClass.forex, "X/Y") == "X/Y"
    with pytest.raises(ProviderUnsupported):
        provider.map_symbol("AAPL", AssetClass.us_equity, None)
    with pytest.raises(ProviderUnsupported):
        provider.map_symbol("BTC-USD", AssetClass.crypto, None)


@respx.mock
async def test_fx_rate_decimal(provider):
    route = respx.get(URL, params={"base": "EUR", "symbols": "USD"}).respond(
        200, text=(FIX / "frankfurter_latest.json").read_text()
    )
    rate = await provider.get_fx_rate("EUR", "USD")
    assert route.called
    assert isinstance(rate, Decimal)
    assert rate == Decimal("1.1081")


@respx.mock
async def test_quote_ts_is_ecb_reference_time(provider):
    respx.get(URL).respond(
        200, json={"amount": 1.0, "base": "EUR", "date": "2026-11-12", "rates": {"USD": 1.1081}}
    )
    q = (await provider.get_quotes([("EURUSD", "EUR/USD")]))["EURUSD"]
    assert q.last == Decimal("1.1081")
    assert q.bid is None and q.ask is None
    assert q.provider == "frankfurter"
    assert q.ts == datetime(2026, 11, 12, 15, 0, tzinfo=UTC)  # 16:00 CET


@respx.mock
async def test_quote_summer_time_offset(provider):
    respx.get(URL).respond(200, json={"date": "2026-07-01", "rates": {"USD": 1.2}})
    q = (await provider.get_quotes([("EURUSD", "EUR/USD")]))["EURUSD"]
    assert q.ts == datetime(2026, 7, 1, 14, 0, tzinfo=UTC)  # 16:00 CEST


@respx.mock
async def test_missing_rate_unsupported(provider):
    respx.get(URL).respond(200, json={"date": "2026-11-12", "rates": {}})
    with pytest.raises(ProviderUnsupported):
        await provider.get_fx_rate("EUR", "USD")


@respx.mock
async def test_http_429_raises(provider):
    respx.get(URL).respond(429)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_fx_rate("EUR", "USD")


@respx.mock
async def test_dated_fx_rate_uses_the_day_endpoint(provider):
    from datetime import date

    route = respx.get("https://api.frankfurter.dev/v1/2027-03-06",
                      params={"base": "USD", "symbols": "EUR"}).respond(
        200, json={"amount": 1.0, "base": "USD", "date": "2027-03-05",
                   "rates": {"EUR": 0.9123}})  # a Saturday: the Friday's rate
    rate = await provider.get_fx_rate_on("USD", "EUR", date(2027, 3, 6))
    assert route.called
    assert rate == Decimal("0.9123")


@respx.mock
async def test_dated_fx_rate_missing_unsupported(provider):
    from datetime import date

    respx.get("https://api.frankfurter.dev/v1/2027-03-06").respond(
        200, json={"date": "2027-03-05", "rates": {}})
    with pytest.raises(ProviderUnsupported):
        await provider.get_fx_rate_on("USD", "EUR", date(2027, 3, 6))
