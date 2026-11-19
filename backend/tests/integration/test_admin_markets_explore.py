"""Admin API «Mercati» (plan 22): ranked search, list filters / quick filters / sort,
facets, the instrument sheet, day changes, the sector map and the movers."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.marketdata.types import Bar, NewsItem

PASSWORD = "correct-horse-battery"
D = Decimal


@pytest.fixture
async def admin(fresh_db, redis_url, monkeypatch):
    from smtrading.admin.auth import hash_password
    from smtrading.admin.models import AdminUser
    from smtrading.api.main import create_app
    from smtrading.engine.quotesource import FakeMarketData
    from smtrading.mcp.runtime import McpRuntime
    from smtrading.seed import run_seed

    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()
    redis = Redis.from_url(redis_url)
    await redis.flushdb()

    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=email, password_hash=await hash_password(PASSWORD)))
        await s.commit()

    qs = FakeMarketData()
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["X-CSRF-Token"] = client.cookies["smt_csrf"]

            class NS:
                pass

            ns = NS()
            ns.client, ns.sm, ns.qs, ns.redis = client, sm, qs, redis
            yield ns
    finally:
        stop.set()
        await lifespan
        await redis.flushdb()
        await redis.aclose()
        await engine.dispose()


async def _search(admin, q, **params):
    r = await admin.client.get("/api/instruments/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_search_ranking(admin):
    hits = await _search(admin, "aapl")
    assert hits[0]["symbol"] == "AAPL" and hits[0]["score"] == 100
    assert hits[0]["match"] == "symbol" and hits[0]["sector"] == "Tecnologia"
    assert hits[0]["indices"] == ["SP500", "NDX"]
    hits = await _search(admin, "apple")
    assert hits[0]["symbol"] == "AAPL" and hits[0]["match"] == "name"
    hits = await _search(admin, "brkb")  # without separators
    assert hits[0]["symbol"] == "BRK-B" and hits[0]["score"] == 95
    hits = await _search(admin, "nv")  # symbol prefix before name contains
    assert hits[0]["symbol"] in ("NVDA", "NVR") and hits[0]["score"] == 90
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    # a name word: "Intesa Sanpaolo" for "sanpaolo"
    assert (await _search(admin, "sanpaolo"))[0]["symbol"] == "ISP.MI"
    # index and sector: every hit belongs, the match says why
    mib = await _search(admin, "ftse mib", limit=50)
    assert mib[0]["symbol"] == "ETFMIB.MI" and mib[0]["match"] == "name"  # the name first
    assert len(mib) == 41 and all(
        "FTSEMIB" in h["indices"] and h["match"] == "index" for h in mib[1:])
    fin = await _search(admin, "finanza", limit=10)
    assert fin and all(h["sector"] == "Finanza" and h["match"] == "sector" for h in fin)
    banks = await _search(admin, "banks - regional", limit=5)
    assert banks and all(h["match"] == "industry" for h in banks)
    assert await _search(admin, "%") == [] and await _search(admin, "") == []
    assert len(await _search(admin, "a", limit=7)) == 7
    r = await admin.client.get("/api/instruments/search", params={"q": "x" * 65})
    assert r.status_code == 400
    r = await admin.client.get("/api/instruments/search", params={"q": "a", "limit": 51})
    assert r.status_code in (400, 422)


async def _list(admin, **params):
    r = await admin.client.get("/api/instruments", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_filters_and_sort(admin):
    from smtrading.instruments.models import Instrument

    page = await _list(admin, index="FTSEMIB", limit=100)
    assert page["total"] == 40 and all("FTSEMIB" in i["indices"] for i in page["items"])
    page = await _list(admin, sector="Tecnologia", asset_class="eu_equity", limit=500)
    assert page["total"] > 3 and all(i["sector"] == "Tecnologia" for i in page["items"])
    assert all(i["asset_class"] == "eu_equity" for i in page["items"])
    asc = [i["symbol"] for i in (await _list(admin, index="DAX", sort="name"))["items"]]
    desc = [i["symbol"] for i in (await _list(admin, index="DAX", sort="name", dir="desc"))[
        "items"]]
    assert len(asc) == 40 and desc == asc[::-1]
    sectors = [i["sector"] for i in (await _list(admin, sort="sector", limit=500))["items"]]
    assert sectors[0] == "Beni di prima necessità" and len(set(sectors)) > 5
    async with admin.sm() as s:
        await s.execute(update(Instrument).where(Instrument.symbol == "AAPL").values(sector=None))
        await s.commit()
    page = await _list(admin, sector="Senza settore")
    assert [i["symbol"] for i in page["items"]] == ["AAPL"]
    # «aggiunti di recente»: created in the last 7 days, or added on demand
    async with admin.sm() as s:
        await s.execute(update(Instrument).values(
            created_at=datetime.now(UTC) - timedelta(days=30)))
        await s.execute(update(Instrument).where(Instrument.symbol == "MSFT").values(
            created_at=datetime.now(UTC) - timedelta(days=2)))
        await s.execute(update(Instrument).where(Instrument.symbol == "IBM").values(
            source="auto"))
        await s.commit()
    recent = await _list(admin, quick="recent", sort="created_at", dir="desc")
    assert [i["symbol"] for i in recent["items"]] == ["MSFT", "IBM"]
    for bad in ({"sector": "Bond"}, {"index": "FTSE"}, {"quick": "hot"},
                {"sort": "price"}, {"dir": "up"}):
        assert (await admin.client.get("/api/instruments", params=bad)).status_code == 400


async def _setup_holdings(admin):
    """A position in AAPL, a resting order on it, a watchlist with ENI.MI and an approved
    strategy on NVDA; the «Segnali» cache says NVDA's entry fires now."""
    from smtrading.activity.models import Decision
    from smtrading.admin.routes_signals import CACHE_KEY
    from smtrading.agents import service as agents
    from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
    from smtrading.portfolios.service import create_portfolio
    from smtrading.positions.models import Position
    from smtrading.watch.service import add_to_watchlist, create_watchlist

    async with admin.sm() as s:
        p = await create_portfolio(s, name="Prudente", initial_cash=D(1000))
        ceo, _ = await agents.create_agent(s, "CEO")
        s.add(Position(portfolio_id=p.id, symbol="AAPL", qty=D(2), avg_price=D(180)))
        s.add(Order(portfolio_id=p.id, symbol="AAPL", side=OrderSide.sell,
                    type=OrderType.stop, qty=D(2), stop_price=D(150),
                    status=OrderStatus.pending))
        wl = await create_watchlist(s, ceo.id, "Europa")
        await add_to_watchlist(s, ceo.id, uuid.UUID(str(wl["id"])), ["ENI.MI"])
        s.add(Decision(agent_id=ceo.id, kind="strategy", summary="Approvata STR-007",
                       portfolio_id=p.id, symbols=["NVDA"], details={
                           "ref": "STR-007", "status": "approvata", "timeframe": "1d",
                           "strategy": "rsi_reversion",
                           "params": {"period": 2, "buy_below": 10, "sell_above": 70}}))
        await s.commit()
    signals = {"as_of": "x", "portfolios": [{"portfolio_id": str(p.id),
               "portfolio_name": "Prudente", "strategies": [{
                   "ref": "STR-007", "name": "Approvata STR-007", "symbols": [
                       {"symbol": "NVDA", "triggered_now": True, "position": None,
                        "open_entry": False, "indicator": "RSI 2", "threshold": "10",
                        "distance": "-1", "direction": "below"},
                       {"symbol": "AAPL", "triggered_now": True, "position": {"qty": "2"},
                        "open_entry": False}]}]}]}
    await admin.redis.set(CACHE_KEY, json.dumps(signals), ex=60)
    return p


async def test_quick_filters(admin):
    await _setup_holdings(admin)

    async def syms(quick):
        return [i["symbol"] for i in (await _list(admin, quick=quick))["items"]]

    assert await syms("portfolio") == ["AAPL"]
    assert await syms("watchlist") == ["ENI.MI"]
    assert await syms("strategy") == ["NVDA"]
    assert await syms("signal") == ["NVDA"]  # AAPL is already held: not a new signal
    page = await _list(admin, quick="portfolio", index="FTSEMIB")
    assert page["total"] == 0 and page["items"] == []


async def test_facets(admin):
    r = await admin.client.get("/api/instruments/facets")
    assert r.status_code == 200
    f = r.json()
    by = {x["sector"]: x["count"] for x in f["sectors"]}
    assert by["ETF"] > 100 and by["Crypto"] > 50 and by["Cambi"] > 20
    assert [x["code"] for x in f["indices"]] == ["SP500", "NDX", "FTSEMIB", "DAX", "CAC40",
                                                  "MDAX"]
    assert {x["code"]: x["count"] for x in f["indices"]}["DAX"] == 40
    assert "MINE" in [u["code"] for u in f["universes"]]
    assert "Leva e inverso" in f["etf_types"] and len(f["sector_list"]) == 14


async def test_overview(admin):
    p = await _setup_holdings(admin)
    r = await admin.client.get("/api/instruments/aapl/overview")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["instrument"]["symbol"] == "AAPL" and o["instrument"]["indices"] == ["SP500", "NDX"]
    assert o["positions"] == [{"portfolio_id": str(p.id), "portfolio_name": "Prudente",
                               "qty": "2", "avg_price": "180", "opened_at": None}]
    assert [(x["side"], x["type"], x["stop_price"]) for x in o["open_orders"]] == [
        ("sell", "stop", "150")]
    assert o["fills"] == []
    assert o["watchlists"] == [] and o["strategies"] == []
    assert o["market_status"]["market"] == "NASDAQ"
    nvda = (await admin.client.get("/api/instruments/NVDA/overview")).json()
    assert [(s["ref"], s["portfolios"]) for s in nvda["strategies"]] == [("STR-007", ["Prudente"])]
    assert nvda["signals"][0]["triggered_now"] is True and nvda["signals"][0]["ref"] == "STR-007"
    assert [d["summary"] for d in nvda["decisions"]] == ["Approvata STR-007"]
    eni = (await admin.client.get("/api/instruments/ENI.MI/overview")).json()
    assert eni["watchlists"] == [{"agent": "CEO", "watchlist": "Europa"}]
    assert (await admin.client.get("/api/instruments/NOPE/overview")).status_code == 404


async def test_overview_fills(admin):
    """Our executions on the symbol, newest first, with the order's role (chart markers)."""
    from smtrading.engine.models import OrderSide, OrderType
    from smtrading.engine.service import PlaceOrderRequest, close_position, place_order
    from smtrading.events import discard_post_commit
    from smtrading.portfolios.service import create_portfolio

    opened = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Tuesday: NASDAQ open
    admin.qs.set_fx("EUR", "USD", D("1.25"))
    admin.qs.set_quote("MSFT", D("399"), D("400"), None)
    async with admin.sm() as s:
        p = await create_portfolio(s, name="Aggressivo", initial_cash=D(20000))
        await place_order(s, admin.qs, PlaceOrderRequest(
            portfolio_id=p.id, symbol="MSFT", side=OrderSide.buy, type=OrderType.market,
            qty=D(10)), now=opened)
        discard_post_commit(s)
        await s.commit()
    admin.qs.set_quote("MSFT", D("419"), D("420"), None)
    async with admin.sm() as s:
        await close_position(s, admin.qs, p.id, "MSFT", D(4), now=opened)
        discard_post_commit(s)
        await s.commit()
    o = (await admin.client.get("/api/instruments/MSFT/overview")).json()
    assert [(f["side"], f["qty"], f["price"], f["portfolio_name"]) for f in o["fills"]] == [
        ("sell", "4", "419", "Aggressivo"), ("buy", "10", "400", "Aggressivo")]
    assert o["fills"][1]["portfolio_id"] == str(p.id) and o["fills"][1]["ts"]
    assert (await admin.client.get("/api/instruments/AAPL/overview")).json()["fills"] == []


async def test_bars_window(admin):
    """The closed bars of a window (the trade chart), OHLCV, validated and bounded."""
    t0 = datetime(2026, 11, 2, tzinfo=UTC)
    admin.qs.set_bars("AAPL", [Bar(t0 + timedelta(hours=i), D(100 + i), D(102 + i),
                                   D(99 + i), D(101 + i), D(1000 + i)) for i in range(48)])
    r = await admin.client.get("/api/instruments/aapl/bars", params={
        "timeframe": "1h", "start": "2026-11-02T10:00:00Z", "end": "2026-11-02T13:00:00+00:00"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["symbol"] == "AAPL" and b["timeframe"] == "1h" and b["error"] is None
    assert [x["open"] for x in b["bars"]] == ["110", "111", "112"]
    assert b["bars"][0] == {"ts": "2026-11-02T10:00:00+00:00", "open": "110", "high": "112",
                            "low": "109", "close": "111", "volume": "1010"}
    # a symbol without data: no bars and the provider's code
    r = await admin.client.get("/api/instruments/MSFT/bars", params={
        "start": "2026-10-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"})
    assert r.status_code == 200 and r.json()["bars"] == []
    assert r.json()["error"] == "SYMBOL_NOT_FOUND"
    for bad in ({"timeframe": "5m"}, {"start": "ieri"}, {"end": "2026-11-01T00:00:00Z"},
                {"timeframe": "1h", "start": "2026-08-01T00:00:00Z"}):
        params = {"timeframe": "1h", "start": "2026-11-02T00:00:00Z",
                  "end": "2026-11-03T00:00:00Z", **bad}
        r = await admin.client.get("/api/instruments/AAPL/bars", params=params)
        assert r.status_code == 400, (bad, r.text)
    r = await admin.client.get("/api/instruments/NOPE/bars", params={
        "start": "2026-11-02T00:00:00Z", "end": "2026-11-03T00:00:00Z"})
    assert r.status_code == 404


async def test_market_sheet(admin):
    now = datetime.now(UTC)
    admin.qs.set_quote("AAPL", D("199"), D("201"), D("200"))
    admin.qs.set_day_change("AAPL", D("200"), D("190"))
    t_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    bars = [Bar(t_end - timedelta(days=80 - i), D(100 + i), D(101 + i), D(99 + i), D(100 + i),
                D(1)) for i in range(80)]
    admin.qs.set_bars("AAPL", bars)
    admin.qs.set_news([NewsItem("n1", now, "Apple sale", None, "https://x", "feed", ("AAPL",))])
    r = await admin.client.get("/api/instruments/AAPL/market")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["quote"]["last"] == "200" and m["day_change"]["change_pct"] == "5.26"
    assert len(m["bars"]) == 80 and m["bars"][-1]["close"] == "179"
    assert {k: m["bars"][-1][k] for k in ("open", "high", "low", "volume")} == {
        "open": "179", "high": "180", "low": "178", "volume": "1"}
    ind = m["indicators"]
    assert ind["close"] == "179" and ind["sma20"] == "169.5" and ind["sma50"] == "154.5"
    assert D(ind["rsi14"]) == 100 and ind["atr14"] is not None
    assert [n["headline"] for n in m["news"]] == ["Apple sale"]
    assert m["next_earnings"] is None and m["errors"].get("bars") is None
    # cached for a minute: a new quote is not seen yet
    admin.qs.set_quote("AAPL", D("1"), D("2"), D("1.5"))
    assert (await admin.client.get("/api/instruments/AAPL/market")).json()["quote"]["last"] == "200"
    # no bars, no hourly data: those parts are null with an error code, the rest answers
    r = await admin.client.get("/api/instruments/MSFT/market", params={"timeframe": "1h"})
    m = r.json()
    assert r.status_code == 200 and m["bars"] == [] and m["indicators"] is None
    assert m["errors"]["bars"] == "SYMBOL_NOT_FOUND"
    r = await admin.client.get("/api/instruments/AAPL/market", params={"timeframe": "5m"})
    assert r.status_code == 400


async def test_day_changes_and_snapshot(admin):
    from smtrading.instruments.models import Instrument

    async with admin.sm() as s:
        mib = sorted(await s.scalars(
            Instrument.__table__.select().with_only_columns(Instrument.symbol)
            .where(Instrument.indices.contains(["FTSEMIB"]))))
        sectors = dict((await s.execute(
            Instrument.__table__.select().with_only_columns(Instrument.symbol, Instrument.sector)
            .where(Instrument.symbol.in_(mib)))).all())
    # 30 of 40 with a change: i-th symbol moves (i - 15) percent
    for i, sym in enumerate(mib[:30]):
        admin.qs.set_day_change(sym, D(100 + i - 15), D(100))
    r = await admin.client.get("/api/markets/day-changes",
                               params={"symbols": f"{mib[0]},{mib[1]},NOPE"})
    assert r.status_code == 200
    assert D(r.json()[mib[0]]["change_pct"]) == -15 and "NOPE" not in r.json()
    assert (await admin.client.get("/api/markets/day-changes",
                                   params={"symbols": ""})).status_code == 400

    r = await admin.client.get("/api/markets/snapshot", params={"universe": "ftsemib"})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert (snap["universe"], snap["label"], snap["total"], snap["covered"]) == (
        "FTSEMIB", "FTSE MIB", 40, 30)
    assert sum(t["count"] for t in snap["sectors"]) == 30
    tile = snap["sectors"][0]
    members = [m["change_pct"] for m in tile["members"]]
    assert members == sorted(members, key=D, reverse=True)
    avg = sum(D(m) for m in members) / len(members)
    assert D(tile["avg_change_pct"]) == avg.quantize(D("0.01"))
    assert all(sectors[m["symbol"]] == tile["sector"] for m in tile["members"])
    assert [m["symbol"] for m in snap["gainers"]] == list(reversed(mib[16:30]))[:10]
    assert [D(m["change_pct"]) for m in snap["losers"][:2]] == [-15, -14]
    assert len(snap["gainers"]) == 10 and len(snap["losers"]) == 10
    # cached (partial coverage: one minute): a new change is not seen yet
    admin.qs.set_day_change(mib[0], D(200), D(100))
    again = (await admin.client.get("/api/markets/snapshot",
                                    params={"universe": "FTSEMIB"})).json()
    assert again == snap
    ttl = await admin.redis.ttl("markets:snapshot:v1:FTSEMIB")
    assert 0 < ttl <= 60

    await _setup_holdings(admin)
    admin.qs.set_day_change("AAPL", D(101), D(100))
    mine = (await admin.client.get("/api/markets/snapshot", params={"universe": "MINE"})).json()
    assert mine["total"] == 2  # AAPL held, ENI.MI watched
    assert mine["covered"] == 1 + ("ENI.MI" in mib[:30])
    assert "AAPL" in [m["symbol"] for m in mine["gainers"] + mine["losers"]]
    assert (await admin.client.get("/api/markets/snapshot",
                                   params={"universe": "LSE"})).status_code == 400


async def test_patch_metadata(admin):
    c = admin.client
    r = await c.patch("/api/instruments/ENI.MI", json={
        "sector": "Materiali", "industry": "Chimica", "indices": ["FTSEMIB", "FTSEMIB"]})
    assert r.status_code == 200, r.text
    assert (r.json()["sector"], r.json()["industry"], r.json()["indices"]) == (
        "Materiali", "Chimica", ["FTSEMIB"])
    for bad in ({"sector": "Bond"}, {"indices": ["XX"]}, {"industry": ""}, {"sector": None}):
        assert (await c.patch("/api/instruments/ENI.MI", json=bad)).status_code == 400
