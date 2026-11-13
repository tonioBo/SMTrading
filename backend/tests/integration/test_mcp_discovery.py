"""On-demand instruments through the MCP tools (fake router with probe_quote)."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.db import make_engine
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.marketdata.types import InstrumentHit
from smtrading.risk.models import Notification
from smtrading.seed import run_seed

D = Decimal


def _hit(symbol, name, ac, exchange):
    return InstrumentHit(symbol, name, ac, exchange, None, "fake")


@pytest.fixture
async def env(mcp_server, mcp_call):
    engine = make_engine(mcp_server.db_url)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await run_seed(s)
        await s.commit()

    qs = mcp_server.qs
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("ZZC-USD", D("1.99"), D("2.01"), D(2))
    qs.set_quote("ZZNEW", D("9.99"), D("10.01"), D(10))
    qs.set_quote("ZZL.L", D(1), D(1), D(1))
    qs.set_search([
        _hit("ZZC-USD", "ZetaCoin", AssetClass.crypto, "CCC"),
        _hit("ZZNEW", "Zeta New Inc.", AssetClass.us_equity, "NASDAQ"),
        _hit("ZZOTC", "Zeta Pink", AssetClass.us_equity, "OTC Markets"),
        _hit("ZZL.L", "Zeta London", AssetClass.eu_equity, "LSE"),
    ])

    async def call(tool, args=None):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args or {})

    async def instrument(symbol):
        async with AsyncSession(engine, expire_on_commit=False) as s:
            return await s.get(Instrument, symbol)

    async def notifications():
        async with AsyncSession(engine) as s:
            return list(await s.scalars(
                select(Notification.payload).where(Notification.kind == "instrument.added")))

    yield {"call": call, "pid": str(mcp_server.demo_id), "instrument": instrument,
           "notifications": notifications}
    await engine.dispose()


async def test_place_order_auto_adds_then_fills(env):
    assert await env["instrument"]("ZZC-USD") is None
    err, order = await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": "zzc-usd", "side": "buy", "type": "market",
        "qty": "10"})
    assert not err, order
    assert order["symbol"] == "ZZC-USD" and order["status"] == "filled"
    inst = await env["instrument"]("ZZC-USD")
    assert inst.source == InstrumentSource.auto and inst.added_by == "auto:test-agent"
    assert inst.market == "CRYPTO" and inst.tick_size == D("0.00000001")
    assert [n["symbol"] for n in await env["notifications"]()] == ["ZZC-USD"]


async def test_unsupported_symbol_is_invalid_order(env):
    err, out = await env["call"]("place_order", {
        "portfolio_id": env["pid"], "symbol": "ZZL.L", "side": "buy", "type": "market",
        "qty": "1"})
    assert err and out["code"] == "INVALID_ORDER"
    assert out["message"].startswith("Strumento non supportato: borsa .L")
    assert await env["instrument"]("ZZL.L") is None
    err, out = await env["call"]("get_instrument", {"symbol": "ZZOTC"})
    assert err and out["code"] == "INVALID_ORDER" and "OTC" in out["message"]


async def test_get_instrument_and_quote_add_on_demand(env):
    err, out = await env["call"]("get_instrument", {"symbol": "ZZNEW"})
    assert not err, out
    assert out["market"] == "NASDAQ" and out["market_status"]["market"] == "NASDAQ"
    err, out = await env["call"]("get_quote", {"symbols": ["ZZNEW", "ZZOTC", "NOPEX"]})
    assert not err
    assert out["ZZNEW"]["last"] == "10"
    assert out["ZZOTC"]["error"] == "INVALID_ORDER" and "OTC" in out["ZZOTC"]["message"]
    assert out["NOPEX"]["error"] == "SYMBOL_NOT_FOUND"


async def test_search_returns_catalog_and_provider_candidates(env):
    err, rows = await env["call"]("search_instruments", {"query": "zeta"})
    assert not err
    by = {r["symbol"]: r for r in rows}
    assert set(by) == {"ZZC-USD", "ZZNEW"}  # OTC and LSE are not addable
    assert by["ZZNEW"]["in_catalogo"] is False and by["ZZNEW"]["aggiungibile"] is True
    assert by["ZZNEW"]["market"] == "NASDAQ"
    assert await env["instrument"]("ZZNEW") is None  # a search creates nothing

    await env["call"]("get_instrument", {"symbol": "ZZNEW"})
    err, rows = await env["call"]("search_instruments", {"query": "zeta"})
    by = {r["symbol"]: r for r in rows}
    assert by["ZZNEW"]["in_catalogo"] is True and by["ZZNEW"]["provider"] == "local"
    assert rows[0]["symbol"] == "ZZNEW"  # catalog rows first

    err, rows = await env["call"]("search_instruments", {"query": "zeta", "market": "CRYPTO"})
    assert [r["symbol"] for r in rows] == ["ZZC-USD"]
    err, rows = await env["call"](
        "search_instruments", {"query": "zeta", "include_provider": False})
    assert [r["symbol"] for r in rows] == ["ZZNEW"]
    err, rows = await env["call"]("search_instruments", {"query": "apple"})
    assert rows[0]["symbol"] == "AAPL" and rows[0]["in_catalogo"] is True
    err, out = await env["call"]("search_instruments", {"query": "zz", "market": "LSE"})
    assert err and out["code"] == "INVALID_ORDER"
    err, out = await env["call"]("search_instruments", {"query": "zz", "limit": 51})
    assert err and out["code"] == "INVALID_ORDER"


async def test_watchlist_alert_and_backtest_add_on_demand(env):
    err, wl = await env["call"]("create_watchlist", {"name": "auto"})
    assert not err, wl
    err, wl = await env["call"]("add_to_watchlist", {"watchlist_id": wl["id"],
                                                     "symbols": ["ZZNEW"]})
    assert not err and wl["symbols"] == ["ZZNEW"]
    err, out = await env["call"]("create_alert", {"symbol": "ZZC-USD",
                                                  "condition": "price_above", "value": "3"})
    assert not err, out
    assert (await env["instrument"]("ZZC-USD")).active
    err, out = await env["call"]("add_to_watchlist", {"watchlist_id": wl["id"],
                                                      "symbols": ["ZZOTC"]})
    assert err and out["code"] == "INVALID_ORDER"
