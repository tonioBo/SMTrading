"""Fixture provenance (no API key available): examples from the official Finnhub API definition
(https://finnhub.io/static/swagger.json, "sampleResponse" of each endpoint, checked 2026-11-13; the
human docs live at https://finnhub.io/docs/api/{quote,company-news,company-profile2,
company-basic-financials,earnings-calendar,websocket-trades}).
- finnhub_quote, finnhub_company_news (2nd item hand-made), finnhub_profile2, finnhub_earnings_calendar:
  sampleResponse of /quote, /company-news, /stock/profile2, /calendar/earnings.
- finnhub_metric: sampleResponse of /stock/metric, with peTTM/epsTTM/dividendYieldIndicatedAnnual
  added (documented metric names, values invented).
- finnhub_ws_trade: trade message of https://finnhub.io/docs/api/websocket-trades
  ({"data":[{"c","p","s","t" (ms),"v"}],"type":"trade"}).
/stock/dividend is a premium endpoint ("Premium Access Required"): not used, dividends stay on Yahoo.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
import websockets

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.finnhub import FinnhubProvider, parse_finnhub_ws
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://finnhub.io/api/v1"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield FinnhubProvider({"api_key": "TOK"}, http)


def test_registered_and_metadata():
    assert PROVIDERS["finnhub"] is FinnhubProvider
    assert FinnhubProvider.capabilities == {
        Capability.quote, Capability.stream, Capability.news, Capability.fundamentals, Capability.calendar}
    assert FinnhubProvider.asset_classes == {AssetClass.us_equity, AssetClass.eu_equity}
    assert FinnhubProvider.requires_key is True
    assert FinnhubProvider.default_rate_limit_per_min == 60


def test_map_symbol(provider):
    assert provider.map_symbol("AAPL", AssetClass.us_equity, None) == "AAPL"
    assert provider.map_symbol("ENI.MI", AssetClass.eu_equity, None) == "ENI.MI"
    assert provider.map_symbol("ENI.MI", AssetClass.eu_equity, "ENI.MI-X") == "ENI.MI-X"
    for symbol, ac in (("BTC-USD", AssetClass.crypto), ("EURUSD=X", AssetClass.forex)):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(symbol, ac, None)


async def test_empty_config_is_safe_and_unsupported():
    async with httpx.AsyncClient() as http:
        p = FinnhubProvider({}, http)
        with pytest.raises(ProviderUnsupported):
            await p.get_quotes([("AAPL", "AAPL")])
        with pytest.raises(ProviderUnsupported):
            await p.health_check()
        with pytest.raises(ProviderUnsupported):
            async for _ in p.stream([("AAPL", "AAPL")]):
                pass


@respx.mock
async def test_quotes(provider):
    route = respx.get(f"{API}/quote").respond(200, text=fixture("finnhub_quote.json"))
    quotes = await provider.get_quotes([("AAPL", "AAPL")])
    params = route.calls.last.request.url.params
    assert params["symbol"] == "AAPL" and params["token"] == "TOK"
    q = quotes["AAPL"]
    assert (q.bid, q.ask, q.last) == (None, None, Decimal("261.74"))
    assert isinstance(q.last, Decimal) and q.provider == "finnhub"
    assert q.ts == datetime.fromtimestamp(1582641000, UTC)


@respx.mock
async def test_quote_all_zeros_is_skipped(provider):
    respx.get(f"{API}/quote").respond(200, json={"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0})
    assert await provider.get_quotes([("ENI.MI", "ENI.MI")]) == {}


@respx.mock
async def test_news_dedupes_and_sorts(provider):
    route = respx.get(f"{API}/company-news").respond(200, text=fixture("finnhub_company_news.json"))
    news = await provider.get_news([("AAPL", "AAPL"), ("MSFT", "MSFT")], 10)
    params = route.calls.last.request.url.params
    assert params["symbol"] == "MSFT" and "from" in params and "to" in params
    assert [n.id for n in news] == ["25286", "25285"]
    assert news[0].symbols == ("AAPL", "MSFT") and news[0].source == "The Economic Times India"
    assert news[0].ts == datetime.fromtimestamp(1569550360, UTC)
    assert news[1].summary is None
    assert len(await provider.get_news([("AAPL", "AAPL")], 1)) == 1


@respx.mock
async def test_fundamentals(provider):
    respx.get(f"{API}/stock/profile2").respond(200, text=fixture("finnhub_profile2.json"))
    metric = respx.get(f"{API}/stock/metric").respond(200, text=fixture("finnhub_metric.json"))
    f = await provider.get_fundamentals("AAPL", "AAPL")
    assert metric.calls.last.request.url.params["metric"] == "all"
    assert f.name == "Apple Inc" and f.industry == "Technology" and f.sector is None
    assert f.market_cap == Decimal(1415993000000) and f.currency == "USD"
    assert (f.pe, f.eps, f.dividend_yield) == (Decimal("28.5"), Decimal("6.05"), Decimal("0.55"))
    assert (f.high_52w, f.low_52w) == (Decimal("310.43"), Decimal("149.22"))
    assert f.extra["beta"] == "1.2989" and f.extra["country"] == "US"


@respx.mock
async def test_fundamentals_unknown_symbol_is_unsupported(provider):
    respx.get(f"{API}/stock/profile2").respond(200, json={})
    with pytest.raises(ProviderUnsupported):
        await provider.get_fundamentals("ZZZZ", "ZZZZ")


@respx.mock
async def test_calendar_earnings(provider):
    route = respx.get(f"{API}/calendar/earnings").respond(200, text=fixture("finnhub_earnings_calendar.json"))
    events = await provider.get_calendar([("AAPL", "AAPL")], date(2019, 10, 1), date(2020, 2, 1))
    params = route.calls.last.request.url.params
    assert (params["symbol"], params["from"], params["to"]) == ("AAPL", "2019-10-01", "2020-02-01")
    assert [(e.kind, e.date) for e in events] == [("earnings", date(2020, 1, 28)), ("earnings", date(2019, 10, 30))]
    assert events[0].data["eps_estimate"] == "4.5474" and events[0].data["hour"] == "amc"
    assert events[0].data["revenue_actual"] == "91819000000"


@respx.mock
async def test_invalid_key_and_health_check(provider):
    route = respx.get(f"{API}/quote").respond(200, text=fixture("finnhub_quote.json"))
    await provider.health_check()
    route.respond(401, json={"error": "Invalid API key"})
    with pytest.raises(RuntimeError, match="chiave non valida"):
        await provider.health_check()
    with pytest.raises(RuntimeError, match="chiave non valida"):
        await provider.get_quotes([("AAPL", "AAPL")])
    route.respond(429)
    with pytest.raises(RuntimeError, match="finnhub: HTTP 429"):
        await provider.get_quotes([("AAPL", "AAPL")])


@respx.mock
async def test_403_plan_not_accessible_is_unsupported_not_invalid_key(provider):
    # free plan on an EU symbol: Finnhub answers 403 with this body
    route = respx.get(f"{API}/quote").respond(
        403, json={"error": "You don't have access to this resource."}
    )
    with pytest.raises(ProviderUnsupported, match="non incluso nel piano Finnhub"):
        await provider.get_quotes([("ENI.MI", "ENI.MI")])
    route.respond(403, text="Premium Access Required")
    with pytest.raises(ProviderUnsupported, match="non incluso nel piano Finnhub"):
        await provider.get_quotes([("ENI.MI", "ENI.MI")])
    route.respond(401, json={"error": "Invalid API key"})
    with pytest.raises(RuntimeError, match="chiave non valida"):
        await provider.get_quotes([("ENI.MI", "ENI.MI")])


@respx.mock
@pytest.mark.parametrize("body", [
    "Access denied: invalid token", "Forbidden. See the explanation in the docs",
    "Plane crashed", "Subscriptions endpoint unavailable for this key",
])
async def test_403_without_a_plan_phrase_is_an_invalid_key(provider, body):
    """Only whole plan phrases mean «not in your plan» (plan 10 review)."""
    respx.get(f"{API}/quote").respond(403, text=body)
    with pytest.raises(RuntimeError, match="chiave non valida"):
        await provider.get_quotes([("ENI.MI", "ENI.MI")])


@respx.mock
@pytest.mark.parametrize("body", [
    "You don\u2019t have access to this resource.", "This endpoint needs a premium plan",
    "Upgrade your subscription", "Not available on your plan.",
])
async def test_403_plan_phrases_are_unsupported(provider, body):
    respx.get(f"{API}/quote").respond(403, text=body)
    with pytest.raises(ProviderUnsupported, match="non incluso nel piano Finnhub"):
        await provider.get_quotes([("ENI.MI", "ENI.MI")])


@respx.mock
async def test_403_plan_on_health_check_leaves_health_untouched(provider):
    from smtrading.marketdata.router import MarketDataRouter

    respx.get(f"{API}/quote").respond(403, json={"error": "You don't have access to this resource."})
    router = MarketDataRouter.__new__(MarketDataRouter)
    assert await router._health("finnhub", provider) == ("finnhub", None, None)


@respx.mock
@pytest.mark.parametrize("status", [429, 500])
async def test_http_errors_never_leak_key(provider, status):
    from smtrading.marketdata.router import _describe

    respx.get(f"{API}/quote").respond(status)
    with pytest.raises(RuntimeError) as ei:
        await provider.get_quotes([("AAPL", "AAPL")])
    assert str(ei.value) == f"finnhub: HTTP {status}"
    assert "TOK" not in _describe(ei.value) and "token" not in _describe(ei.value)


@respx.mock
async def test_transport_error_never_leaks_key(provider):
    respx.get(f"{API}/quote").mock(side_effect=httpx.ConnectError("boom https://x/quote?token=TOK"))
    with pytest.raises(RuntimeError) as ei:
        await provider.get_quotes([("AAPL", "AAPL")])
    assert "TOK" not in str(ei.value) and "ConnectError" in str(ei.value)


def test_parse_finnhub_ws_zero_price_skipped():
    msg = {"type": "trade", "data": [{"s": "X", "p": 0, "t": 1}]}
    assert parse_finnhub_ws(msg, {"X": "X"}) == []


def test_parse_finnhub_ws_trade():
    msg = json.loads(fixture("finnhub_ws_trade.json"), parse_float=Decimal)
    [q] = parse_finnhub_ws(msg, {"BINANCE:BTCUSDT": "BTC-USD"})
    assert q.symbol == "BTC-USD" and q.last == Decimal("7296.89") and q.bid is None
    assert isinstance(q.last, Decimal) and q.provider == "finnhub"
    assert q.ts == datetime.fromtimestamp(1575526691.134, UTC)


def test_parse_finnhub_ws_ignores_noise():
    rev = {"AAPL": "AAPL"}
    assert parse_finnhub_ws({"type": "ping"}, rev) == []
    assert parse_finnhub_ws({"type": "trade", "data": [{"s": "MSFT", "p": 1, "t": 1}]}, rev) == []
    assert parse_finnhub_ws({"type": "trade", "data": [{"s": "AAPL", "t": 1}]}, rev) == []


async def test_stream_subscribe_and_two_quotes():
    received: list[dict] = []

    async def handler(ws):
        received.append(json.loads(await ws.recv()))
        received.append(json.loads(await ws.recv()))
        await ws.send('{"type":"ping"}')
        await ws.send('{"data":[{"p":172.5,"s":"AAPL","t":1763000000000,"v":10}],"type":"trade"}')
        await ws.send('{"data":[{"p":911.5,"s":"ENI.MI","t":1763000001000,"v":1}],"type":"trade"}')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = FinnhubProvider({"api_key": "TOK", "ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in p.stream([("AAPL", "AAPL"), ("ENI.MI", "ENI.MI")]):
                    quotes.append(q)
    assert received == [{"type": "subscribe", "symbol": "AAPL"}, {"type": "subscribe", "symbol": "ENI.MI"}]
    assert [(q.symbol, q.last) for q in quotes] == [("AAPL", Decimal("172.5")), ("ENI.MI", Decimal("911.5"))]


async def test_stream_error_message_raises():
    async def handler(ws):
        await ws.recv()
        await ws.send('{"type":"error","msg":"Subscribing to too many symbols"}')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = FinnhubProvider({"api_key": "TOK", "ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError, match="too many"):
                async for _ in p.stream([("AAPL", "AAPL")]):
                    pass


async def test_stream_invalid_token_rejected_is_connection_error():
    from http import HTTPStatus

    async def reject(connection, request):
        return connection.respond(HTTPStatus.UNAUTHORIZED, "no\n")

    async def handler(ws):  # pragma: no cover - never reached
        pass

    async with websockets.serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = FinnhubProvider({"api_key": "bad", "ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError):
                async for _ in p.stream([("AAPL", "AAPL")]):
                    pass


async def test_stream_handshake_failure_is_connection_error():
    async with httpx.AsyncClient() as http:
        p = FinnhubProvider({"api_key": "TOK", "ws_url": "ws://127.0.0.1:1"}, http)
        with pytest.raises(ConnectionError):
            async for _ in p.stream([("AAPL", "AAPL")]):
                pass


@respx.mock
async def test_quote_without_timestamp_is_skipped(provider):
    respx.get(f"{API}/quote").respond(200, json={"c": 10, "t": 0})
    assert await provider.get_quotes([("AAPL", "AAPL")]) == {}


def test_parse_finnhub_ws_without_timestamp_skipped():
    msg = {"type": "trade", "data": [{"s": "AAPL", "p": 10}]}
    assert parse_finnhub_ws(msg, {"AAPL": "AAPL"}) == []
