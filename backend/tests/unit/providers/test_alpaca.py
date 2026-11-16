"""Fixture provenance (no API keys available, so no recorded responses): examples from the official
Alpaca Market Data docs, checked 2026-11-13.
- alpaca_stock_quotes_latest / alpaca_stock_trades_latest:
  https://docs.alpaca.markets/reference/stocklatestquotes-1 , .../stocklatesttrades-1
- alpaca_stock_bars(+_page2): https://docs.alpaca.markets/reference/stockbars (page2 hand-made, same shape)
- alpaca_crypto_quotes_latest / alpaca_crypto_trades_latest:
  https://docs.alpaca.markets/reference/cryptolatestquotes-1 , .../cryptolatesttrades-1
- alpaca_crypto_bars: https://docs.alpaca.markets/reference/cryptobars-1
- alpaca_news: https://docs.alpaca.markets/reference/news-3
- alpaca_ws_stock_quote: https://docs.alpaca.markets/docs/real-time-stock-pricing-data
- alpaca_ws_crypto_quote: https://docs.alpaca.markets/docs/real-time-crypto-pricing-data
"""

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
from smtrading.marketdata.providers.alpaca import AlpacaProvider, parse_alpaca_ws
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
API = "https://data.alpaca.markets"
CONFIG = {"key_id": "KID", "secret_key": "SEC"}


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield AlpacaProvider(dict(CONFIG), http)


def test_registered_and_metadata():
    assert PROVIDERS["alpaca"] is AlpacaProvider
    assert AlpacaProvider.capabilities == {
        Capability.quote, Capability.bars, Capability.stream, Capability.news}
    assert AlpacaProvider.asset_classes == {AssetClass.us_equity, AssetClass.crypto}
    assert AlpacaProvider.requires_key is True
    assert AlpacaProvider.config_fields == ("key_id", "secret_key")  # read by _credentials
    assert AlpacaProvider.default_rate_limit_per_min == 200


def test_map_symbol(provider):
    assert provider.map_symbol("AAPL", AssetClass.us_equity, None) == "AAPL"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "BTC/USD"
    assert provider.map_symbol("AAPL", AssetClass.us_equity, "XYZ") == "XYZ"
    for symbol, ac in (("ENI.MI", AssetClass.eu_equity), ("EURUSD=X", AssetClass.forex),
                       ("BTC", AssetClass.crypto)):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(symbol, ac, None)


async def test_empty_config_is_safe_and_unsupported():
    async with httpx.AsyncClient() as http:
        p = AlpacaProvider({}, http)
        with pytest.raises(ProviderUnsupported):
            await p.get_quotes([("AAPL", "AAPL")])
        with pytest.raises(ProviderUnsupported):
            await p.health_check()
        with pytest.raises(ProviderUnsupported):
            async for _ in p.stream([("AAPL", "AAPL")]):
                pass


@respx.mock
async def test_stock_quotes(provider):
    q = respx.get(f"{API}/v2/stocks/quotes/latest").respond(200, text=fixture("alpaca_stock_quotes_latest.json"))
    t = respx.get(f"{API}/v2/stocks/trades/latest").respond(200, text=fixture("alpaca_stock_trades_latest.json"))
    quotes = await provider.get_quotes([("AAPL", "AAPL"), ("TSLA", "TSLA")])
    req = q.calls.last.request
    assert req.headers["APCA-API-KEY-ID"] == "KID" and req.headers["APCA-API-SECRET-KEY"] == "SEC"
    assert req.url.params["symbols"] == "AAPL,TSLA" and req.url.params["feed"] == "iex"
    assert t.called
    aapl = quotes["AAPL"]
    assert (aapl.bid, aapl.ask, aapl.last) == (Decimal("172.62"), Decimal("172.7"), Decimal("172.78"))
    assert isinstance(aapl.bid, Decimal) and aapl.provider == "alpaca"
    assert aapl.ts == datetime(2022, 8, 17, 10, 7, 40, 286587, tzinfo=UTC)
    assert quotes["TSLA"].last is None and quotes["TSLA"].bid == Decimal("911.3")


@respx.mock
async def test_feed_from_config():
    respx.get(f"{API}/v2/stocks/quotes/latest").respond(200, json={"quotes": {}})
    trades = respx.get(f"{API}/v2/stocks/trades/latest").respond(200, json={"trades": {}})
    async with httpx.AsyncClient() as http:
        p = AlpacaProvider({**CONFIG, "feed": "sip"}, http)
        assert await p.get_quotes([("AAPL", "AAPL")]) == {}
    assert trades.calls.last.request.url.params["feed"] == "sip"


@respx.mock
async def test_crypto_quotes(provider):
    respx.get(f"{API}/v1beta3/crypto/us/latest/quotes").respond(200, text=fixture("alpaca_crypto_quotes_latest.json"))
    respx.get(f"{API}/v1beta3/crypto/us/latest/trades").respond(200, text=fixture("alpaca_crypto_trades_latest.json"))
    quotes = await provider.get_quotes([("BTC-USD", "BTC/USD")])
    btc = quotes["BTC-USD"]
    assert (btc.bid, btc.ask, btc.last) == (Decimal(29058), Decimal(29059), Decimal(29791))
    assert set(quotes) == {"BTC-USD"}


@respx.mock
async def test_stock_bars_paginated(provider):
    route = respx.get(f"{API}/v2/stocks/bars").mock(side_effect=[
        httpx.Response(200, text=fixture("alpaca_stock_bars.json")),
        httpx.Response(200, text=fixture("alpaca_stock_bars_page2.json")),
    ])
    start = datetime(2022, 1, 3, 9, 0, tzinfo=UTC)
    bars = await provider.get_bars("AAPL", "AAPL", Timeframe.m1, start, datetime(2022, 1, 3, 10, tzinfo=UTC))
    p0, p1 = (c.request.url.params for c in route.calls)
    assert p0["timeframe"] == "1Min" and p0["symbols"] == "AAPL" and p0["feed"] == "iex"
    assert p0["start"] == "2022-01-03T09:00:00Z" and p0["end"] == "2022-01-03T10:00:00Z"
    assert "page_token" not in p0 and p1["page_token"].startswith("QUFQ")
    assert [b.close for b in bars] == [Decimal("178.21"), Decimal("178.3"), Decimal("178.5")]
    assert bars[0].volume == Decimal(1118) and bars[0].ts == start


@respx.mock
async def test_crypto_bars_and_timeframes(provider):
    route = respx.get(f"{API}/v1beta3/crypto/us/bars").respond(200, text=fixture("alpaca_crypto_bars.json"))
    now = datetime.now(UTC)
    bars = await provider.get_bars("BTC-USD", "BTC/USD", Timeframe.h1, now, now)
    assert bars[0].volume == Decimal("0.01") and bars[0].high == Decimal(29003)
    assert "feed" not in route.calls.last.request.url.params
    for tf, expected in (("5m", "5Min"), ("15m", "15Min"), ("1h", "1Hour"), ("1d", "1Day"), ("1wk", "1Week")):
        await provider.get_bars("BTC-USD", "BTC/USD", Timeframe(tf), now, now)
        assert route.calls.last.request.url.params["timeframe"] == expected


@respx.mock
async def test_news(provider):
    route = respx.get(f"{API}/v1beta1/news").respond(200, text=fixture("alpaca_news.json"))
    [n] = await provider.get_news([("TSLA", "TSLA"), ("BTC-USD", "BTC/USD")], 5)
    params = route.calls.last.request.url.params
    assert params["symbols"] == "TSLA,BTCUSD" and params["limit"] == "5"
    assert n.id == "62429279" and n.source == "benzinga" and n.symbols == ("TSLA",)
    assert n.ts == datetime(2026, 10, 18, 12, 51, 2, tzinfo=UTC)


@respx.mock
async def test_health_check_ok_and_invalid_key(provider):
    route = respx.get(f"{API}/v2/stocks/quotes/latest").respond(200, json={"quotes": {}})
    await provider.health_check()
    assert route.calls.last.request.url.params["symbols"] == "SPY"
    for status in (401, 403):
        route.respond(status, json={"message": "forbidden."})
        with pytest.raises(RuntimeError, match="chiave non valida"):
            await provider.health_check()


@respx.mock
async def test_server_error_propagates(provider):
    respx.get(f"{API}/v2/stocks/quotes/latest").respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_quotes([("AAPL", "AAPL")])


@respx.mock
async def test_router_falls_back_after_401(fresh_db, redis_url, monkeypatch):
    """Review Focus 3: a rejected key makes the router serve the quote from the next provider."""
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.common.db import make_engine
    from smtrading.connectors.models import Connector, ProviderPriority
    from smtrading.instruments.models import Instrument
    from smtrading.marketdata.provider import DataProvider
    from smtrading.marketdata.router import MarketDataRouter
    from smtrading.marketdata.types import Quote

    class Second(DataProvider):
        name = "fake_second"
        capabilities = frozenset({Capability.quote})
        asset_classes = frozenset({AssetClass.us_equity})

        def _default_symbol(self, symbol, asset_class):
            return symbol

        async def get_quotes(self, items):
            return {s: Quote(s, Decimal(1), Decimal(2), Decimal("1.5"), datetime.now(UTC), self.name)
                    for s, _ in items}

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    monkeypatch.setitem(PROVIDERS, "fake_second", Second)
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add_all([
            Connector(name="alpaca", enabled=True),
            Connector(name="fake_second", enabled=True),
            ProviderPriority(asset_class=AssetClass.us_equity, capability=Capability.quote,
                             providers=["alpaca", "fake_second"]),
            Instrument(symbol="AAPL", name="Apple", asset_class=AssetClass.us_equity,
                       market="us", currency="USD"),
        ])
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    respx.route(host="data.alpaca.markets").respond(401, json={"message": "unauthorized."})
    respx.route(host__regex=".*").pass_through()
    try:
        async with httpx.AsyncClient() as http:
            router = MarketDataRouter(sm, redis, http)
            quotes = await router.get_quotes(["AAPL"])
    finally:
        await redis.aclose()
        await engine.dispose()
    assert quotes["AAPL"].provider == "fake_second"


def test_parse_alpaca_ws_stock_and_crypto():
    [q] = parse_alpaca_ws(json.loads(fixture("alpaca_ws_stock_quote.json"), parse_float=Decimal))
    assert q.symbol == "AMD" and (q.bid, q.ask) == (Decimal("91.95"), Decimal("91.98"))
    assert isinstance(q.bid, Decimal) and q.provider == "alpaca" and q.last is None
    assert q.ts == datetime(2023, 4, 6, 11, 54, 21, 670905, tzinfo=UTC)
    [c] = parse_alpaca_ws(json.loads(fixture("alpaca_ws_crypto_quote.json"), parse_float=Decimal))
    assert c.symbol == "BAT-USD" and c.ask == Decimal("0.3581")
    [m] = parse_alpaca_ws(json.loads(fixture("alpaca_ws_crypto_quote.json")), {"BAT/USD": "BAT-USD"})
    assert m.symbol == "BAT-USD"


def test_parse_alpaca_ws_ignores_noise():
    rev = {"AMD": "AMD"}
    msgs = [
        {"T": "success", "msg": "connected"},
        {"T": "subscription", "quotes": ["AMD"]},
        {"T": "t", "S": "AMD", "p": 1},
        {"T": "q", "S": "NVDA", "bp": 1, "ap": 2},
        {"T": "q", "bp": 1},
        "junk",
    ]
    assert parse_alpaca_ws(msgs, rev) == []
    [q] = parse_alpaca_ws([{"T": "q", "S": "AMD", "bp": 0, "ap": 2, "t": "2023-04-06T11:54:21Z"}], rev)
    assert q.bid is None and q.ask == Decimal(2)


async def test_stream_auth_subscribe_two_quotes():
    received: list[dict] = []

    async def handler(ws):
        await ws.send('[{"T":"success","msg":"connected"}]')
        received.append(json.loads(await ws.recv()))
        await ws.send('[{"T":"success","msg":"authenticated"}]')
        received.append(json.loads(await ws.recv()))
        await ws.send('[{"T":"subscription","quotes":["AAPL","TSLA"]}]')
        await ws.send('[{"T":"q","S":"AAPL","bp":172.6,"ap":172.7,"t":"2026-11-13T10:00:00Z"},'
                      '{"T":"q","S":"TSLA","bp":911.3,"ap":911.6,"t":"2026-11-13T10:00:01Z"}]')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = AlpacaProvider({**CONFIG, "ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in p.stream([("AAPL", "AAPL"), ("TSLA", "TSLA")]):
                    quotes.append(q)
    assert received == [
        {"action": "auth", "key": "KID", "secret": "SEC"},
        {"action": "subscribe", "quotes": ["AAPL", "TSLA"], "trades": ["AAPL", "TSLA"]},
    ]
    assert [q.symbol for q in quotes] == ["AAPL", "TSLA"] and quotes[1].ask == Decimal("911.6")


async def test_stream_crypto_maps_symbols_back():
    async def handler(ws):
        await ws.send('[{"T":"success","msg":"connected"}]')
        await ws.recv()
        await ws.send('[{"T":"success","msg":"authenticated"}]')
        sub = json.loads(await ws.recv())
        assert sub["quotes"] == ["BTC/USD"] and sub["trades"] == ["BTC/USD"]
        await ws.send(fixture("alpaca_ws_crypto_quote.json"))  # BAT/USD: not subscribed, dropped
        await ws.send('[{"T":"q","S":"BTC/USD","bp":1,"ap":2,"t":"2026-11-13T10:00:00Z"}]')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = AlpacaProvider({**CONFIG, "ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in p.stream([("BTC-USD", "BTC/USD")]):
                    quotes.append(q)
    assert [q.symbol for q in quotes] == ["BTC-USD"]


async def test_stream_auth_error_is_connection_error():
    async def handler(ws):
        await ws.send('[{"T":"success","msg":"connected"}]')
        await ws.recv()
        await ws.send('[{"T":"error","code":402,"msg":"auth failed"}]')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = AlpacaProvider({**CONFIG, "ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError, match="auth failed"):
                async for _ in p.stream([("AAPL", "AAPL")]):
                    pass


async def test_stream_handshake_failure_is_connection_error():
    async with httpx.AsyncClient() as http:
        p = AlpacaProvider({**CONFIG, "ws_url": "ws://127.0.0.1:1"}, http)
        with pytest.raises(ConnectionError):
            async for _ in p.stream([("AAPL", "AAPL")]):
                pass


def test_parse_alpaca_ws_merges_trades_and_quotes_per_symbol():
    rev = {"SPY": "SPY", "QQQ": "QQQ"}
    state: dict = {}
    # a trade before any quote: last only
    [a] = parse_alpaca_ws([{"T": "t", "S": "SPY", "i": 1, "x": "V", "p": 741.05, "s": 100,
                            "t": "2026-11-16T15:00:00.1Z", "c": ["@"], "z": "B"}], rev, state)
    assert (a.bid, a.ask, a.last) == (None, None, Decimal("741.05"))
    assert a.ts == datetime(2026, 11, 16, 15, 0, 0, 100000, tzinfo=UTC)
    # a quote keeps the last known trade price
    [b] = parse_alpaca_ws([{"T": "q", "S": "SPY", "bx": "V", "bp": 741.0, "bs": 2, "ax": "V",
                            "ap": 741.1, "as": 3, "t": "2026-11-16T15:00:01Z", "c": ["R"], "z": "B"}],
                          rev, state)
    assert (b.bid, b.ask, b.last) == (Decimal("741.0"), Decimal("741.1"), Decimal("741.05"))
    assert b.ts == datetime(2026, 11, 16, 15, 0, 1, tzinfo=UTC)
    # a trade keeps the last known bid/ask; symbols do not mix
    [c, d] = parse_alpaca_ws([{"T": "t", "S": "SPY", "p": 741.2, "t": "2026-11-16T15:00:02Z"},
                              {"T": "q", "S": "QQQ", "bp": 610, "ap": 611, "t": "2026-11-16T15:00:02Z"}],
                             rev, state)
    assert (c.symbol, c.bid, c.ask, c.last) == ("SPY", Decimal("741.0"), Decimal("741.1"), Decimal("741.2"))
    assert c.ts == datetime(2026, 11, 16, 15, 0, 2, tzinfo=UTC)
    assert (d.symbol, d.last) == ("QQQ", None)
    # a late (older) message never moves the time backwards
    [e] = parse_alpaca_ws([{"T": "q", "S": "SPY", "bp": 740.9, "ap": 741.3, "t": "2026-11-16T14:59:59Z"}],
                          rev, state)
    assert (e.bid, e.last, e.ts) == (Decimal("740.9"), Decimal("741.2"), datetime(2026, 11, 16, 15, 0, 2, tzinfo=UTC))
    # a trade without a valid price or time is dropped
    assert parse_alpaca_ws([{"T": "t", "S": "SPY", "p": 0, "t": "2026-11-16T15:00:03Z"},
                            {"T": "t", "S": "SPY", "p": 742}], rev, state) == []


async def test_stream_trades_fill_last_keeping_bid_ask():
    async def handler(ws):
        await ws.send('[{"T":"success","msg":"connected"}]')
        await ws.recv()
        await ws.send('[{"T":"success","msg":"authenticated"}]')
        await ws.recv()
        await ws.send('[{"T":"subscription","trades":["SPY"],"quotes":["SPY"]}]')
        await ws.send('[{"T":"q","S":"SPY","bp":741.0,"ap":741.1,"t":"2026-11-16T15:00:00Z"}]')
        await ws.send('[{"T":"t","S":"SPY","i":52983525029461,"x":"V","p":741.05,"s":10,'
                      '"t":"2026-11-16T15:00:01Z","c":["@","I"],"z":"B"}]')

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = AlpacaProvider({**CONFIG, "ws_url": f"ws://127.0.0.1:{port}"}, http)
            quotes = []
            with pytest.raises(ConnectionError):
                async for q in p.stream([("SPY", "SPY")]):
                    quotes.append(q)
    assert [(q.bid, q.ask, q.last) for q in quotes] == [
        (Decimal("741.0"), Decimal("741.1"), None),
        (Decimal("741.0"), Decimal("741.1"), Decimal("741.05")),
    ]


def test_quotes_need_a_valid_timestamp(provider):
    assert parse_alpaca_ws([{"T": "q", "S": "AAPL", "bp": 1, "ap": 2, "t": "bad"}]) == []
    assert parse_alpaca_ws([{"T": "q", "S": "AAPL", "bp": 1, "ap": 2}]) == []
    built = provider._build([("AAPL", "AAPL")], {"AAPL": {"bp": 1, "ap": 2}}, {"AAPL": {"p": 1.5}})
    assert built == {}
    built = provider._build([("AAPL", "AAPL")], {"AAPL": {"bp": 1, "ap": 2,
                                                         "t": "2026-11-10T15:00:00Z"}}, {})
    assert built["AAPL"].ts == datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
