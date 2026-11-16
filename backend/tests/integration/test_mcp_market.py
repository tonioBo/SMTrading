"""MCP market data tools on the live fixture server (FakeMarketData as the router)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.db import make_engine
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.types import Bar, CalendarEvent, Fundamentals, InstrumentHit, NewsItem
from smtrading.seed import run_seed

D = Decimal
SYM = "BTC-USD"
NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def _bars(n: int, step=timedelta(days=1)) -> list[Bar]:
    return [
        Bar(NOW - step * (n - i), D(100 + i), D(101 + i), D(99 + i), D(100 + i), D(10))
        for i in range(n)
    ]


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        s.add(Instrument(symbol="ZZINACT", name="Dormant Co", asset_class=AssetClass.us_equity,
                         market="NYSE", currency="USD", active=False))
        await s.commit()
    await engine.dispose()
    qs = mcp_server.qs
    qs.set_quote(SYM, D("49999.5"), D("50000.5"), D("50000"))

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    return {"call": call, "qs": qs}


async def test_tools_listed(mcp_server, mcp_list_tools):
    names = set(await mcp_list_tools(mcp_server.url, mcp_server.token))
    assert {
        "search_instruments", "get_instrument", "get_quote", "get_bars", "get_market_status",
        "get_indicators", "get_fx_rate", "get_news", "get_fundamentals",
        "get_earnings_calendar", "get_dividends", "screen",
    } <= names


async def test_search_merges_local_and_provider(env):
    env["qs"].set_search([
        InstrumentHit("BTC-USD", "Bitcoin", AssetClass.crypto, "CCC", "USD", "fake"),
        InstrumentHit("BTCX-USD", "Bitcoin Xtra", AssetClass.crypto, "CCC", "USD", "fake"),
        InstrumentHit("BTCQ", "Not addable", AssetClass.crypto, "CCC", "USD", "fake"),
        InstrumentHit("ZZINACT", "Dormant Co", AssetClass.us_equity, "NYSE", "USD", "fake"),
    ])
    err, rows = await env["call"]("search_instruments", {"query": "btc"})
    assert not err
    by = {r["symbol"]: r for r in rows}
    assert by["BTC-USD"]["tradable"] is True and by["BTC-USD"]["provider"] == "local"
    assert by["BTC-USD"]["in_catalogo"] is True
    assert by["BTCX-USD"]["tradable"] is False and by["BTCX-USD"]["provider"] == "fake"
    assert by["BTCX-USD"]["in_catalogo"] is False and by["BTCX-USD"]["aggiungibile"] is True
    assert "BTCQ" not in by  # a provider hit that could not be added is left out
    assert len([r for r in rows if r["symbol"] == "BTC-USD"]) == 1

    err, rows = await env["call"]("search_instruments", {"query": "dormant"})
    assert [(r["symbol"], r["tradable"], r["in_catalogo"]) for r in rows] == [
        ("ZZINACT", False, True)]

    err, rows = await env["call"](
        "search_instruments", {"query": "btc", "asset_class": "forex"})
    assert not err and rows == []

    err, rows = await env["call"]("search_instruments", {"query": "%"})
    assert not err and rows == []  # wildcard is escaped


async def test_get_instrument(env):
    err, out = await env["call"]("get_instrument", {"symbol": SYM})
    assert not err
    assert out["symbol"] == SYM and out["market_status"]["market"] == "CRYPTO"
    assert out["market_status"]["is_open"] is True
    err, out = await env["call"]("get_instrument", {"symbol": " btc-usd "})
    assert not err and out["symbol"] == SYM
    err, out = await env["call"]("get_instrument", {"symbol": "NOPE"})
    assert err and out["code"] == "SYMBOL_NOT_FOUND"


async def test_get_quote_and_isolated_errors(env):
    err, out = await env["call"]("get_quote", {"symbols": [SYM]})
    assert not err and out[SYM]["last"] == "50000" and out[SYM]["bid"] == "49999.5"
    err, out = await env["call"]("get_quote", {"symbols": [SYM, "MISSING"]})
    assert not err
    assert out[SYM]["last"] == "50000"
    assert out["MISSING"]["error"] == "SYMBOL_NOT_FOUND"  # unknown to the providers too


async def test_get_quote_limit_and_validation(env):
    err, out = await env["call"]("get_quote", {"symbols": [f"S{i}" for i in range(51)]})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"]("get_quote", {"symbols": []})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"]("get_quote", {"symbols": [f"S{i}" for i in range(50)]})
    assert not err and len(out) == 50 and all("error" in v for v in out.values())


async def test_get_bars_default_range_and_limit(env):
    env["qs"].set_bars(SYM, _bars(320))
    err, bars = await env["call"]("get_bars", {"symbol": SYM, "timeframe": "1d"})
    assert not err
    assert 0 < len(bars) <= 301  # 300 calendar days back
    assert bars[0]["ts"] < bars[-1]["ts"] and isinstance(bars[0]["open"], str)
    err, bars = await env["call"]("get_bars", {"symbol": SYM, "timeframe": "1d", "limit": 5})
    assert [b["close"] for b in bars] == ["415", "416", "417", "418", "419"]
    frm = (NOW - timedelta(days=10)).isoformat()
    err, bars = await env["call"](
        "get_bars", {"symbol": SYM, "timeframe": "1d", "from_ts": frm})
    assert len(bars) == 10
    err, out = await env["call"]("get_bars", {"symbol": SYM, "timeframe": "1d", "limit": 1001})
    assert err and out["code"] == "INVALID_ORDER"


async def test_invalid_timeframe(env):
    err, out = await env["call"]("get_bars", {"symbol": SYM, "timeframe": "2d"})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"](
        "get_indicators", {"symbol": SYM, "timeframe": "bad", "indicators": [{"name": "sma"}]})
    assert err and out["code"] == "INVALID_ORDER"


async def test_market_status(env):
    err, out = await env["call"]("get_market_status", {})
    assert not err and {m["market"] for m in out} >= {"NASDAQ", "CRYPTO", "FX"}
    err, out = await env["call"]("get_market_status", {"market": "crypto"})
    assert not err and out["is_open"] is True and out["next_close"] is None
    err, out = await env["call"]("get_market_status", {"market": "MARS"})
    assert err and out["code"] == "NOT_FOUND"


async def test_get_indicators(env):
    env["qs"].set_bars(SYM, _bars(120))
    err, out = await env["call"]("get_indicators", {
        "symbol": SYM, "timeframe": "1d", "lookback": 5,
        "indicators": [{"name": "sma", "params": {"period": 10}}, {"name": "rsi"}],
    })
    assert not err
    assert out["symbol"] == SYM and len(out["ts"]) == 5
    assert len(out["sma"]) == 5 and len(out["rsi"]) == 5
    assert D(out["sma"][-1]) == D("214.5")  # mean of closes 210..219
    assert out["specs"] == [
        {"key": "sma", "name": "sma", "params": {"period": 10}},
        {"key": "rsi", "name": "rsi", "params": {}},
    ]
    err, out = await env["call"]("get_indicators", {
        "symbol": SYM, "timeframe": "1d", "lookback": 5,
        "indicators": [{"name": "sma", "params": {"period": 10}},
                       {"name": "sma", "params": {"period": 20}}],
    })
    assert not err
    assert [s["key"] for s in out["specs"]] == ["sma_10", "sma_20"]
    assert D(out["sma_10"][-1]) == D("214.5") and len(out["sma_20"]) == 5
    for bad in ([{"name": "nope"}], [{"name": "sma", "params": {"period": "x"}}], [],
                [{"name": "sma", "params": {"period": 10**30}}],
                [{"name": "macd", "params": {"slow": 1001}}]):
        err, out = await env["call"](
            "get_indicators", {"symbol": SYM, "timeframe": "1d", "indicators": bad})
        assert err and out["code"] == "INVALID_ORDER", bad


async def test_get_fx_rate(env):
    env["qs"].set_fx("EUR", "USD", D("1.25"))
    err, out = await env["call"]("get_fx_rate", {"base": "eur", "quote": "USD"})
    assert not err and out == {"base": "EUR", "quote": "USD", "rate": "1.25"}
    err, out = await env["call"]("get_fx_rate", {"base": "EUR", "quote": "JPY"})
    assert err and out["code"] == "PROVIDER_UNAVAILABLE"


async def test_news_fundamentals(env):
    env["qs"].set_news([
        NewsItem("1", NOW - timedelta(hours=2), "old", None, None, "wire", (SYM,)),
        NewsItem("2", NOW - timedelta(hours=1), "new", "s", "http://x", "wire", (SYM,)),
        NewsItem("3", NOW, "other", None, None, "wire", ("AAPL",)),
    ])
    err, out = await env["call"]("get_news", {"symbols": [SYM], "limit": 10})
    assert not err and [n["headline"] for n in out] == ["new", "old"]
    assert out[0]["symbols"] == [SYM] and out[0]["ts"].endswith("+00:00")
    err, out = await env["call"]("get_news", {})  # nothing held: empty
    assert not err and out == []

    env["qs"].set_fundamentals("AAPL", Fundamentals(
        "AAPL", "Apple", "Tech", None, D("3000000000000"), D("30.5"), None, D("0.5"),
        None, None, "USD"))
    err, out = await env["call"]("get_fundamentals", {"symbol": "AAPL"})
    assert not err and out["pe"] == "30.5" and out["sector"] == "Tech"
    err, out = await env["call"]("get_fundamentals", {"symbol": "ZZZ"})
    assert err and out["code"] == "SYMBOL_NOT_FOUND"


async def test_calendar_and_dividends(env):
    today = NOW.date()
    env["qs"].set_calendar([
        CalendarEvent("AAPL", "earnings", today + timedelta(days=3), {"eps": "1.2"}),
        CalendarEvent("AAPL", "dividend", today + timedelta(days=5), {"amount": "0.24"}),
        CalendarEvent("AAPL", "dividend", today - timedelta(days=400), {"amount": "0.2"}),
        CalendarEvent("MSFT", "earnings", today + timedelta(days=4)),
    ])
    args = {"from_date": today.isoformat(), "to_date": (today + timedelta(days=30)).isoformat()}
    err, out = await env["call"]("get_earnings_calendar", {**args, "symbols": ["AAPL", "MSFT"]})
    assert not err and [e["symbol"] for e in out] == ["AAPL", "MSFT"]
    assert out[0]["kind"] == "earnings" and out[0]["date"] == (today + timedelta(days=3)).isoformat()
    err, out = await env["call"]("get_dividends", {"symbol": "AAPL"})
    assert not err and [e["data"]["amount"] for e in out] == ["0.24"]
    err, out = await env["call"](
        "get_dividends", {"symbol": "AAPL", "from_date": "2000-01-01", "to_date": "2100-01-01"})
    assert len(out) == 2
    err, out = await env["call"]("get_earnings_calendar", {"from_date": "nope", "to_date": "x"})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"](
        "get_earnings_calendar", {"from_date": "2026-02-01", "to_date": "2026-01-01"})
    assert err and out["code"] == "INVALID_ORDER"
    assert date.fromisoformat(args["from_date"]) == today


async def test_screen(env):
    env["qs"].set_screen([{"symbol": f"S{i}", "pe": D(i)} for i in range(5)])
    err, out = await env["call"]("screen", {"limit": 3, "max_pe": "20", "asset_class": "us_equity"})
    assert not err and [r["symbol"] for r in out] == ["S0", "S1", "S2"]
    assert out[1]["pe"] == "1"
    err, out = await env["call"]("screen", {"asset_class": "bonds"})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"]("screen", {"min_volume": "lots"})
    assert err and out["code"] == "INVALID_ORDER"


async def test_get_indicators_include_forming(env):
    """include_forming: the bar «se chiudesse ora» from the quote (crypto: always open),
    with the closed-bar values next to it; nothing provisional on a stale quote or an
    intraday timeframe; default false = the closed bars only."""
    now = datetime.now(UTC).replace(microsecond=0)  # NOW is the import time: may be stale
    day = timedelta(days=1)
    today = now.replace(hour=0, minute=0, second=0)
    env["qs"].set_bars(SYM, [Bar(today - day * (30 - i), D(100), D(101), D(99), D(100 + i),
                                 D(10)) for i in range(30)])  # closes 100..129, to yesterday
    env["qs"].set_quote(SYM, None, None, D("229"), now)
    args = {"symbol": SYM, "timeframe": "1d", "lookback": 3,
            "indicators": [{"name": "sma", "params": {"period": 10}}]}

    err, out = await env["call"]("get_indicators", args)
    assert not err and "provisional" not in out and "closed" not in out
    assert D(out["sma"][-1]) == D("124.5")  # closes 120..129

    err, out = await env["call"]("get_indicators", {**args, "include_forming": True})
    assert not err, out
    assert out["provisional"] is True
    assert datetime.fromisoformat(out["as_of"]) == now
    assert D(out["sma"][-1]) == D("135.4")  # closes 121..129 + 229
    assert datetime.fromisoformat(out["ts"][-1]) == today
    assert D(out["closed"]["sma"][-1]) == D("124.5")
    assert datetime.fromisoformat(out["closed"]["ts"][-1]) == today - day

    env["qs"].set_quote(SYM, None, None, D("229"), now - timedelta(hours=1))  # crypto: 60 s
    err, out = await env["call"]("get_indicators", {**args, "include_forming": True})
    assert not err and out["provisional"] is False
    assert out["provisional_reason"] == "stale quote" and "closed" not in out
    assert D(out["sma"][-1]) == D("124.5")

    env["qs"].set_quote(SYM, None, None, D("229"), now)
    err, out = await env["call"](
        "get_indicators", {**args, "timeframe": "1h", "include_forming": True})
    assert not err and out["provisional"] is False
    assert out["provisional_reason"] == "timeframe not supported"
