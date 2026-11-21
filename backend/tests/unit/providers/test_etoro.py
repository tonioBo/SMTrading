"""eToro market-data provider (plan 9 Task 14).

Fixture provenance (no key available; shapes of https://api-portal.etoro.com, consulted
2026-11-15):
- etoro_search: `GET /api/v1/market-data/search` (`items[]{instrumentId,
  internalSymbolFull, isCurrentlyTradable, isBuyEnabled, isDelisted}`), with a partial
  match (`AAPL.US`) listed first; ids invented.
- etoro_rates: `GET /api/v1/market-data/instruments/rates` (`rates[]{instrumentID, bid,
  ask, lastExecution, conversionRateBid, conversionRateAsk, date, priceRateID}` plus the
  obsolete `unitMargin*` / `*Discounted`, never read); values invented.
- etoro_candles: `GET .../instruments/{id}/history/candles/desc/OneHour/{count}`
  (`{interval, candles:[{instrumentId, candles:[{instrumentID, fromDate, open, high, low,
  close, volume}], rangeOpen, ...}]}`), mixed casings, one `fromDate` without offset and
  one candle without `high`; values invented.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

import smtrading.marketdata.providers  # noqa: F401
from smtrading.brokers.etoro_core import instrument_cache
from smtrading.brokers.live import LIVE_PATHS
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers import etoro
from smtrading.marketdata.providers.etoro import EtoroProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability, Timeframe

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
HOST = "https://public-api.etoro.com"
MD = HOST + "/api/v1/market-data"
API_KEY = "CANARYAPIKEY0001"
USER_KEY = "CANARYUSERKEY-do-not-use"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _search_item(symbol: str, iid: int, delisted: bool = False) -> dict:
    return {"instrumentId": iid, "internalSymbolFull": symbol, "isCurrentlyTradable": True,
            "isBuyEnabled": True, "isDelisted": delisted}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """A controllable monotonic clock and wall clock; a sleep that fails the test."""
    t = {"mono": 1000.0}
    monkeypatch.setattr(etoro, "_clock", lambda: t["mono"])
    monkeypatch.setattr(etoro, "_now", lambda: NOW)
    etoro._blocked_until.clear()

    async def no_sleep(*_a, **_k):  # Review Focus 7: the provider never waits
        raise AssertionError("etoro provider must not sleep")

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    yield t
    etoro._blocked_until.clear()


@pytest.fixture
async def provider():
    async with httpx.AsyncClient() as http:
        yield EtoroProvider({"api_key": API_KEY, "user_key": USER_KEY}, http)


def test_registered_and_metadata():
    assert PROVIDERS["etoro"] is EtoroProvider
    assert EtoroProvider.capabilities == {Capability.quote, Capability.bars}
    assert EtoroProvider.asset_classes == {
        AssetClass.us_equity, AssetClass.eu_equity, AssetClass.crypto}
    assert EtoroProvider.requires_key is True
    assert EtoroProvider.config_fields == ("api_key", "user_key")
    assert EtoroProvider.default_rate_limit_per_min == 60


def test_symbol_mapping_us_crypto_override(provider):
    assert provider.map_symbol("AAPL", AssetClass.us_equity, None) == "AAPL"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "BTC"
    assert provider.map_symbol("AAPL", AssetClass.us_equity, "AAPL.X") == "AAPL.X"
    # the catalog's EU listings (Milan, Xetra, Paris): eToro's own symbol is the same
    for symbol in ("A2A.MI", "SAP.DE", "AI.PA"):
        assert provider.map_symbol(symbol, AssetClass.eu_equity, None) == symbol
    for symbol, ac in (("ENI", AssetClass.eu_equity), ("VOD.L", AssetClass.eu_equity),
                       ("EURUSD=X", AssetClass.forex), ("BTC-EUR", AssetClass.crypto)):
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(symbol, ac, None)


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_headers_and_market_data_path_only(provider):
    search = respx.get(f"{MD}/search").mock(
        return_value=httpx.Response(200, json=fixture("etoro_search.json")))
    rates = respx.get(f"{MD}/instruments/rates").mock(
        return_value=httpx.Response(200, json=fixture("etoro_rates.json")))
    await provider.get_quotes([("AAPL", "AAPL")])
    assert search.called and rates.called
    ids = set()
    for call in respx.calls:
        h = call.request.headers
        assert h["x-api-key"] == API_KEY and h["x-user-key"] == USER_KEY
        ids.add(str(uuid.UUID(h["x-request-id"])))
        assert "authorization" not in h
        assert call.request.method == "GET"
        assert call.request.url.path.startswith("/api/v1/market-data/")
    assert len(ids) == len(respx.calls)  # a new request id each time
    q = search.calls[0].request.url.params
    assert q["internalSymbolFull"] == "AAPL" and q["fields"]


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_non_market_data_path_refused_before_network(provider):
    # the real execution/info prefixes come from LIVE_PATHS (never spelled outside
    # brokers/live: test_live_hosts_guard)
    for path in (*(f"{p}/pnl" for p in sorted(LIVE_PATHS)),
                 "/api/v1/market-data/../trading/info/pnl",
                 "/api/v1/market-data/%2e%2e/trading/info/pnl",
                 "/api/v1/market-data/./search", "/api/v1/market-data\\..\\trading",
                 "/api/v1/market-data/search#x", "/api/v1/market-data/sea rch",
                 "api/v1/market-data/search", "/api/v1/market-data//search"):
        with pytest.raises(RuntimeError, match="percorso non ammesso"):
            await provider._get(path)
    assert not respx.calls


async def test_without_keys_unsupported():
    async with httpx.AsyncClient() as http:
        for config in ({}, {"api_key": API_KEY}, {"user_key": USER_KEY}):
            p = EtoroProvider(config, http)
            with pytest.raises(ProviderUnsupported):
                await p.get_quotes([("AAPL", "AAPL")])


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_instrument_lookup_exact_match_and_cache(provider, _clock):
    def search(request):
        sym = request.url.params["internalSymbolFull"]
        if sym == "AAPL":
            return httpx.Response(200, json=fixture("etoro_search.json"))
        if sym == "OLD":
            return httpx.Response(200, json={"items": [_search_item("OLD", 77, delisted=True)]})
        return httpx.Response(200, json={"items": []})

    route = respx.get(f"{MD}/search").mock(side_effect=search)
    ids = await provider._instrument_ids(["AAPL", "NOPE", "OLD"])
    assert ids == {"AAPL": 1001}  # the identical symbol, not the partial match
    assert route.call_count == 3
    assert await provider._instrument_ids(["AAPL", "NOPE", "OLD"]) == {"AAPL": 1001}
    assert route.call_count == 3  # positive and negative caches
    assert instrument_cache("data").fresh("AAPL")["instrument_id"] == 1001

    _clock["mono"] += 3601  # the negative cache lasts 1 h, the positive one 24 h
    await provider._instrument_ids(["AAPL", "NOPE"])
    assert route.call_count == 4
    assert [c.request.url.params["internalSymbolFull"] for c in route.calls][-1] == "NOPE"


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_lookups_capped_per_call(provider):
    route = respx.get(f"{MD}/search").mock(side_effect=lambda r: httpx.Response(200, json={
        "items": [_search_item(r.url.params["internalSymbolFull"], 5000 + route.call_count)]}))
    symbols = [f"S{i:02d}" for i in range(25)]
    first = await provider._instrument_ids(symbols)
    assert route.call_count == etoro.MAX_LOOKUPS_PER_CALL == 10
    assert set(first) == set(symbols[:10])
    second = await provider._instrument_ids(symbols)
    assert route.call_count == 20 and set(second) == set(symbols[:20])


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_quotes_batch_of_100_and_mapping(provider):
    cache = instrument_cache("data")
    pairs = [(f"S{i:03d}", f"S{i:03d}") for i in range(150)]
    for i, (_, ps) in enumerate(pairs):
        cache.put(ps, 10_000 + i)
    cache.put("AAPL", 1001)

    def rates(request):
        ids = [int(x) for x in request.url.params["instrumentIds"].split(",")]
        assert len(ids) <= 100
        return httpx.Response(200, json={"rates": [
            {"instrumentID": iid, "bid": 1.5, "ask": 1.7, "lastExecution": 1.6,
             "conversionRateBid": 1, "conversionRateAsk": 1,
             "date": "2026-11-15T11:59:00Z"} for iid in ids]})

    route = respx.get(f"{MD}/instruments/rates").mock(side_effect=rates)
    out = await provider.get_quotes(pairs)
    assert route.call_count == 2 and len(out) == 150
    q = out["S042"]
    assert (q.bid, q.ask, q.last) == (Decimal("1.5"), Decimal("1.7"), Decimal("1.6"))
    assert q.ts == datetime(2026, 11, 15, 11, 59, tzinfo=UTC) and q.provider == "etoro"

    route.side_effect = None
    route.return_value = httpx.Response(200, json=fixture("etoro_rates.json"))
    cache.put("BTC", 100000)
    out = await provider.get_quotes([("AAPL", "AAPL"), ("BTC-USD", "BTC")])
    assert out["AAPL"].last == Decimal("189.28") and out["AAPL"].bid == Decimal("189.21")
    assert out["AAPL"].ts == datetime(2026, 11, 15, 15, 30, 12, 345000, tzinfo=UTC)
    assert out["BTC-USD"].last == Decimal("64280.0") and out["BTC-USD"].symbol == "BTC-USD"


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_quote_skipped_without_date_or_non_usd_conversion(provider):
    cache = instrument_cache("data")
    for i, sym in enumerate("ABCDEFG"):
        cache.put(sym, i + 1)
    base = {"bid": 10, "ask": 11, "lastExecution": 10.5, "date": "2026-11-15T11:00:00Z"}
    respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(200, json={"rates": [
        {**base, "instrumentID": 1},  # ok
        {**base, "instrumentID": 2, "date": None},  # no time: never now()
        {**base, "instrumentID": 3, "conversionRateBid": 1.08, "conversionRateAsk": 1.09},
        {**base, "instrumentID": 4, "lastExecution": 0},
        {**base, "instrumentID": 5, "lastExecution": None},
        {**base, "instrumentID": 6, "conversionRateAsk": 0.92},
        {**base, "instrumentID": 999},  # not asked
        {**base, "instrumentId": 7, "date": "2026-11-15T11:00:00"},  # other casing, no tz
    ]}))
    out = await provider.get_quotes([(s, s) for s in "ABCDEFG"])
    # D/E: no last execution (normal outside trading hours, verified on the real API)
    # → the bid/ask mid; no time or a non-USD conversion still skips the quote
    assert set(out) == {"A", "D", "E", "G"}
    assert out["D"].last == out["E"].last == Decimal("10.5")
    assert out["G"].ts == datetime(2026, 11, 15, 11, 0, tzinfo=UTC)


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_bars_interval_count_and_window(provider):
    instrument_cache("data").put("AAPL", 1001)
    route = respx.get(url__regex=rf"{MD}/instruments/1001/history/candles/.*").mock(
        return_value=httpx.Response(200, json=fixture("etoro_candles.json")))
    start = NOW - timedelta(days=3)
    bars = await provider.get_bars("AAPL", "AAPL", Timeframe.h1, start, NOW)
    assert route.calls[0].request.url.path == (
        "/api/v1/market-data/instruments/1001/history/candles/desc/OneHour/73")
    assert [b.ts for b in bars] == [
        datetime(2026, 11, 12, 12, tzinfo=UTC),
        datetime(2026, 11, 14, 10, tzinfo=UTC),  # no offset: read as UTC
        datetime(2026, 11, 15, 11, tzinfo=UTC),
    ]
    b = bars[-1]
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal("189.0"), Decimal("190.2"), Decimal("188.8"), Decimal("190.0"), Decimal(1500))
    assert isinstance(b.open, Decimal)

    # a weekly range: OneWeek, ceil(30 days / 7 days) + 1 = 6
    await provider.get_bars("AAPL", "AAPL", Timeframe.w1, NOW - timedelta(days=30), NOW)
    assert route.calls[1].request.url.path.endswith("/desc/OneWeek/6")


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_bars_over_1000_candles_unsupported(provider):
    instrument_cache("data").put("AAPL", 1001)
    with pytest.raises(ProviderUnsupported):
        await provider.get_bars("AAPL", "AAPL", Timeframe.m1, NOW - timedelta(days=1), NOW)
    assert not respx.calls
    respx.get(f"{MD}/search").mock(return_value=httpx.Response(200, json={"items": []}))
    with pytest.raises(ProviderUnsupported):
        await provider.get_bars("ZZZ", "ZZZ", Timeframe.d1, NOW - timedelta(days=5), NOW)


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_ratelimit_remaining_zero_blocks_without_sleep(provider, _clock):
    instrument_cache("data").put("AAPL", 1001)
    route = respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(
        200, json=fixture("etoro_rates.json"),
        headers={"RateLimit-Limit": "60", "RateLimit-Remaining": "0", "RateLimit-Reset": "20",
                 "RateLimit-Policy": "60;w=60"}))
    assert "AAPL" in await provider.get_quotes([("AAPL", "AAPL")])  # the answer is kept
    with pytest.raises(RuntimeError, match="etoro: rate limited"):
        await provider.get_quotes([("AAPL", "AAPL")])
    assert route.call_count == 1  # no network while blocked
    # the block is per user key, shared by every instance of the module
    async with httpx.AsyncClient() as http:
        other = EtoroProvider({"api_key": API_KEY, "user_key": USER_KEY}, http)
        with pytest.raises(RuntimeError, match="rate limited"):
            await other.get_quotes([("AAPL", "AAPL")])
    _clock["mono"] += 21
    route.return_value = httpx.Response(200, json=fixture("etoro_rates.json"))
    assert "AAPL" in await provider.get_quotes([("AAPL", "AAPL")])
    assert route.call_count == 2


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_429_retry_after_blocks(provider, _clock):
    instrument_cache("data").put("AAPL", 1001)
    route = respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(
        429, json={"errorMessage": "Too many requests"}, headers={"Retry-After": "600"}))
    with pytest.raises(RuntimeError, match="etoro: rate limited"):
        await provider.get_quotes([("AAPL", "AAPL")])
    with pytest.raises(RuntimeError, match="etoro: rate limited"):
        await provider.get_quotes([("AAPL", "AAPL")])
    assert route.call_count == 1
    _clock["mono"] += 61  # Retry-After 600 is capped at 60 s
    route.return_value = httpx.Response(200, json=fixture("etoro_rates.json"))
    assert "AAPL" in await provider.get_quotes([("AAPL", "AAPL")])


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_errors_redacted_no_keys(provider):
    instrument_cache("data").put("AAPL", 1001)
    route = respx.get(f"{MD}/instruments/rates")
    cases = [
        httpx.Response(401, json={"errorMessage": f"bad key {API_KEY}"}),
        httpx.Response(403, json={"errorMessage": "forbidden"}),
        httpx.Response(500, json={"errorMessage": f"internal error for {USER_KEY}"}),
        httpx.ConnectError(f"connect failed x-user-key={USER_KEY}"),
    ]
    messages = []
    for case in cases:
        if isinstance(case, Exception):
            route.mock(side_effect=case)
        else:
            route.mock(return_value=case)
        with pytest.raises(RuntimeError) as e:
            await provider.get_quotes([("AAPL", "AAPL")])
        messages.append(str(e.value))
        assert e.value.__cause__ is None  # nothing chained (the request, its headers)
        assert e.value.__context__ is None or e.value.__suppress_context__
    assert messages[0] == messages[1] == "etoro: chiave non valida"
    assert messages[2] == "etoro: HTTP 500"  # never the broker's text
    assert messages[3] == "etoro: request failed: ConnectError"
    for m in messages:
        for word in (API_KEY, USER_KEY, "x-api-key", "x-user-key", "demo", "/api/"):
            assert word.lower() not in m.lower(), m


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_health_check(provider):
    route = respx.get(f"{MD}/search").mock(
        return_value=httpx.Response(200, json=fixture("etoro_search.json")))
    await provider.health_check()
    instrument_cache("data").clear()
    route.return_value = httpx.Response(200, json={"items": [_search_item("AAPL.US", 9)]})
    with pytest.raises(RuntimeError, match="strumento di riferimento non trovato"):
        await provider.health_check()


@respx.mock
async def test_broker_text_never_forwarded(provider, caplog):
    """I1: eToro's body text (which may name its environment) never reaches the error."""
    import logging

    caplog.set_level(logging.DEBUG)
    instrument_cache("data").put("AAPL", 1001)
    respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(
        400, json={"errorMessage": f"not available in Demo environment {USER_KEY}"}))
    with pytest.raises(RuntimeError) as e:
        await provider.get_quotes([("AAPL", "AAPL")])
    assert str(e.value) == "etoro: HTTP 400"
    assert USER_KEY not in caplog.text


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_quote_without_last_execution_uses_mid(provider):
    """Real eToro answers (verified 2026-11-15 with a real key): outside trading hours
    `lastExecution` is 0.0 while bid/ask are valid → the quote uses the bid/ask mid."""
    instrument_cache("data").put("AAPL", 1001)
    respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(200, json={"rates": [
        {"instrumentID": 1001, "bid": "356.26", "ask": "356.28", "lastExecution": "0.0",
         "conversionRateBid": "1.0", "conversionRateAsk": "1.0",
         "date": "2026-11-13T20:59:55.937Z"}]}))
    out = await provider.get_quotes([("AAPL", "AAPL")])
    assert out["AAPL"].last == Decimal("356.27")
    assert (out["AAPL"].bid, out["AAPL"].ask) == (Decimal("356.26"), Decimal("356.28"))


@respx.mock  # assert_all_mocked (default): any unmocked request fails
async def test_eu_listing_quote_in_its_own_currency(provider):
    """Live check 2026-11-21: A2A.MI rate {bid 2.199, ask 2.2, lastExecution 2.199,
    conversionRate* 1.098} — the price is in EUR (the listing's currency, as Yahoo's 2.194)
    and the conversion rate is EUR→USD: an EU listing keeps it, a US one is still skipped."""
    cache = instrument_cache("data")
    cache.put("A2A.MI", 1271)
    cache.put("AAPL", 1001)
    conv = {"conversionRateBid": 1.09839, "conversionRateAsk": 1.0984,
            "date": "2026-11-20T16:23:56.8003035Z"}
    respx.get(f"{MD}/instruments/rates").mock(return_value=httpx.Response(200, json={"rates": [
        {"instrumentID": 1271, "bid": 2.199, "ask": 2.2, "lastExecution": 2.199, **conv},
        {"instrumentID": 1001, "bid": 180, "ask": 181, "lastExecution": 180.5, **conv},
    ]}))
    out = await provider.get_quotes([("A2A.MI", "A2A.MI"), ("AAPL", "AAPL")])
    assert set(out) == {"A2A.MI"}
    q = out["A2A.MI"]
    assert (q.bid, q.ask, q.last) == (Decimal("2.199"), Decimal("2.2"), Decimal("2.199"))
    assert q.provider == "etoro"
