"""«Mercati v2» (plan 30): the overview service (fake Yahoo, Redis cache, the closed-market
TTL, the stale fallback, the quota gate), favourites per user, the screener job
(idempotent, quota-gated) and endpoint, fundamentals (parsing, 12 h cache, router
fallback), the calendar, the news, the multi-period sector map, and the owner's personal
price alerts (sent once, queued to Telegram, admins only, invisible to agents)."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.config import get_settings
from smtrading.common.db import make_engine
from smtrading.marketdata.types import Bar, Fundamentals, NewsItem
from smtrading.marketsportal.overview import CLOSED_TTL_S, OPEN_TTL_S, SNAPSHOT_KEY, OverviewService
from smtrading.marketsportal.yahoo import PortalYahoo, PortalYahooUnavailable

PASSWORD = "correct-horse-battery"
D = Decimal
SAT = datetime(2026, 11, 21, 12, tzinfo=UTC)  # every market of the bar closed but crypto
TUE = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)  # NYSE and Europe open


class FakeRaw:
    """The raw Yahoo adapter: canned v7 quote / spark / v8 chart answers, calls counted."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.fail_with: str = "HTTP 500"
        self.fail_symbols: set[str] = set()  # a chunk holding one of them fails
        self.rows: dict[str, dict] = {}

    def quote(self, symbols, fields):
        self.calls.append("quote:" + ",".join(symbols))
        if self.fail or self.fail_symbols & set(symbols):
            raise RuntimeError(self.fail_with)
        out = []
        for s in symbols:
            if s in self.rows:
                out.append({"symbol": s, **self.rows[s]})
            elif s.startswith("^") or "=" in s or s.endswith("-USD") or s == "FTSEMIB.MI":
                out.append({"symbol": s, "regularMarketPrice": 110.0,
                            "regularMarketPreviousClose": 100.0,
                            "regularMarketTime": 1795210554, "currency": "USD"})
        return out

    def spark(self, symbols, range_, interval):
        self.calls.append(f"spark:{range_}:{len(symbols)}")
        if self.fail:
            raise RuntimeError("HTTP 500")
        return {s: {"timestamp": [1, 2, 3],
                    "indicators": {"quote": [{"close": [100.0, None, 110.0]}]}}
                for s in symbols}

    def chart(self, symbol, range_, interval):
        self.calls.append(f"chart:{symbol}:{range_}")
        return {"timestamp": [10, 20], "indicators": {"quote": [{"close": [1.0, 2.0]}]}}


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


# ------------------------------------------------------------------ overview service


async def test_overview_snapshot_cache_and_ttl(redis):
    raw = FakeRaw()
    svc = OverviewService(redis, PortalYahoo(redis, None, raw=raw), clock=lambda: SAT)
    snap = await svc.snapshot()
    spx = next(i for i in snap["items"] if i["code"] == "SPX")
    assert spx["value"] == "110.0" and spx["change_pct"] == "10.00"
    assert spx["is_open"] is False and spx["next_open"].startswith("2026-11-23")
    assert spx["spark"] == [[1, 100.0], [3, 110.0]]
    btc = next(i for i in snap["items"] if i["code"] == "BTC")
    assert btc["is_open"] is True  # crypto, yet it does not keep the short TTL
    assert snap["ttl_s"] == CLOSED_TTL_S
    assert 0 < await redis.ttl(SNAPSHOT_KEY) <= CLOSED_TTL_S
    # one quote request for all 15 symbols; sparks: intraday for the open (crypto), 5 days
    assert raw.calls == ["quote:" + ",".join(i["symbol"] for i in snap["items"]),
                         "spark:1d:1", "spark:5d:14"]
    again = await svc.snapshot()  # cached: no call
    assert again == snap and len(raw.calls) == 3

    await redis.flushdb()
    open_svc = OverviewService(redis, PortalYahoo(redis, None, raw=raw), clock=lambda: TUE)
    snap = await open_svc.snapshot()
    assert snap["ttl_s"] == OPEN_TTL_S and 0 < await redis.ttl(SNAPSHOT_KEY) <= OPEN_TTL_S
    assert next(i for i in snap["items"] if i["code"] == "SPX")["is_open"] is True
    prices = await open_svc.prices()
    assert prices["SPX"] == D("110.0") and len(prices) == 15


async def test_overview_stale_and_quota_gate(redis):
    from smtrading.marketdata import quota

    raw = FakeRaw()
    svc = OverviewService(redis, PortalYahoo(redis, None, raw=raw), clock=lambda: SAT)
    await svc.snapshot()
    await redis.delete(SNAPSHOT_KEY)
    raw.fail = True
    stale = await svc.snapshot()  # the last good one, marked
    assert stale["stale"] is True and stale["items"][0]["value"] == "110.0"
    await redis.flushdb()
    none = await svc.snapshot()
    assert none["error"] == "non disponibile" and all(i["value"] is None for i in none["items"])
    # a 429 demotion of Yahoo: the portal asks nothing
    await redis.flushdb()
    raw.fail, n = False, len(raw.calls)
    await quota.block(redis, "yahoo", quota.Budget(60, 2000, None))
    with pytest.raises(PortalYahooUnavailable):
        await PortalYahoo(redis, None, raw=raw).quotes(["^GSPC"], ["symbol"])
    assert len(raw.calls) == n


async def test_overview_counts_yahoo_quota_and_rate_limit(redis):
    from smtrading.marketdata import quota

    raw = FakeRaw()
    y = PortalYahoo(redis, None, raw=raw)
    await y.quotes(["^GSPC"], ["symbol"])
    used = (await quota.usage(redis, ["yahoo"]))["yahoo"]
    assert used.minute == 1 and used.hour == 1
    # the shared bucket empty: refused without a request
    await redis.hset("rl:yahoo", mapping={"tokens": "0", "ts": str(10**10)})
    with pytest.raises(PortalYahooUnavailable):
        await y.chart("^GSPC", "1d", "5m")
    assert not any(c.startswith("chart") for c in raw.calls)


async def test_portal_429_never_demotes_the_trading_provider(redis):
    """A 429 on the portal's v7/v8 endpoints (stricter than the trading side's calls) pauses
    the portal only: Yahoo stays first for the quotes and bars that drive the stops."""
    from smtrading.marketdata import quota

    raw = FakeRaw()
    raw.fail, raw.fail_with = True, "HTTP Error 429: Too Many Requests"
    y = PortalYahoo(redis, None, raw=raw)
    with pytest.raises(PortalYahooUnavailable):
        await y.quotes(["^GSPC"], ["symbol"])
    used = (await quota.usage(redis, ["yahoo"]))["yahoo"]
    assert used.blocked_until is None  # the trading side is untouched
    raw.fail, n = False, len(raw.calls)
    with pytest.raises(PortalYahooUnavailable):  # the portal itself waits
        await y.chart("^GSPC", "1d", "5m")
    assert len(raw.calls) == n


async def test_failed_chunk_is_not_cached_as_empty(redis):
    """A refused or failing v7 chunk is not «unknown to Yahoo»: only the symbols of the
    chunks that answered are cached empty; the others are asked again next time."""
    from types import SimpleNamespace

    from smtrading.instruments.models import AssetClass
    from smtrading.marketsportal.fundamentals import KEY, FundamentalsService
    from smtrading.marketsportal.yahoo import QUOTE_CHUNK

    raw = FakeRaw()
    syms = [f"S{i:03d}" for i in range(QUOTE_CHUNK + 5)]
    raw.rows = {"S001": {"trailingPE": 10.0}, f"S{QUOTE_CHUNK + 1:03d}": {"trailingPE": 12.0}}
    raw.fail_symbols = {"S000"}  # the first chunk fails
    instruments = [SimpleNamespace(symbol=s, provider_symbols={},
                                   asset_class=AssetClass.us_equity) for s in syms]
    svc = FundamentalsService(redis, PortalYahoo(redis, None, raw=raw))
    out = await svc.many(instruments)
    assert set(out) == set(syms[QUOTE_CHUNK:])  # the failed chunk: absent, not empty
    assert out[f"S{QUOTE_CHUNK + 1:03d}"]["source"] == "yahoo"
    assert out[f"S{QUOTE_CHUNK:03d}"]["source"] is None  # answered chunk, unknown symbol
    assert await redis.get(KEY + "S001") is None
    raw.fail_symbols = set()
    out = await svc.many(instruments)
    assert out["S001"]["source"] == "yahoo" and set(out) == set(syms)


async def test_personal_alert_prices_never_stale(redis):
    """The overview's stale fallback (the last good snapshot, kept 7 days) is for the page
    only: the personal alerts get no price rather than one days old."""
    raw = FakeRaw()
    svc = OverviewService(redis, PortalYahoo(redis, None, raw=raw), clock=lambda: TUE)
    assert (await svc.prices())["SPX"] == D("110.0")
    await redis.delete(SNAPSHOT_KEY)
    raw.fail = True
    assert (await svc.snapshot())["stale"] is True
    assert await svc.prices() == {}
    # a fresh-looking snapshot that is old (as_of) is not used either
    await redis.flushdb()
    raw.fail = False
    late = OverviewService(redis, PortalYahoo(redis, None, raw=raw),
                           clock=lambda: TUE - timedelta(hours=2))
    await late.snapshot()
    svc_now = OverviewService(redis, PortalYahoo(redis, None, raw=raw), clock=lambda: TUE)
    assert await svc_now.prices() == {}


# ------------------------------------------------------------------ API fixture


@pytest.fixture
async def portal(fresh_db, redis_url, monkeypatch):
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
    owner = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    viewer = f"viewer-{uuid.uuid4().hex[:8]}@example.com"
    async with sm() as s:
        await run_seed(s)
        s.add(AdminUser(email=owner, password_hash=await hash_password(PASSWORD)))
        s.add(AdminUser(email=viewer, password_hash=await hash_password(PASSWORD),
                        role="viewer"))
        await s.commit()
    qs = FakeMarketData()
    raw = FakeRaw()
    app = create_app(runtime_factory=lambda sm_, redis_: McpRuntime(sm_, redis_, qs, qs))
    app.state.markets_v2_raw = raw
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    lifespan = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(started.wait(), 15)

    async def login(email):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
        return c

    try:
        client, view = await login(owner), await login(viewer)

        class NS:
            pass

        ns = NS()
        ns.client, ns.viewer, ns.sm, ns.qs, ns.redis, ns.raw = client, view, sm, qs, redis, raw
        yield ns
        await client.aclose()
        await view.aclose()
    finally:
        stop.set()
        await lifespan
        await redis.flushdb()
        await redis.aclose()
        await engine.dispose()


async def test_overview_endpoints(portal):
    r = await portal.client.get("/api/markets/overview")
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 15
    r = await portal.viewer.get("/api/markets/overview/SPX/chart", params={"range": "1y"})
    assert r.status_code == 200 and r.json()["points"] == [[10, 1.0], [20, 2.0]]
    assert (await portal.client.get("/api/markets/overview/XXX/chart")).status_code == 404
    r = await portal.client.get("/api/markets/overview/SPX/chart", params={"range": "2y"})
    assert r.status_code == 400
    # never in the trading catalog
    from smtrading.instruments.models import Instrument

    async with portal.sm() as s:
        assert await s.scalar(select(func.count()).select_from(Instrument).where(
            Instrument.symbol.in_(["^GSPC", "FTSEMIB.MI", "GC=F", "SPX"]))) == 0


# ------------------------------------------------------------------ favourites


async def test_favorites_per_user(portal):
    c, v = portal.client, portal.viewer
    assert (await c.get("/api/markets/favorites")).json() == {"items": []}
    for s in ("AAPL", "msft", "ENI.MI"):
        r = await c.put(f"/api/markets/favorites/{s}")
        assert r.status_code == 200, r.text
    assert [i["symbol"] for i in r.json()["items"]] == ["AAPL", "MSFT", "ENI.MI"]
    r = await c.put("/api/markets/favorites/AAPL")  # twice: once
    assert len(r.json()["items"]) == 3
    assert (await c.put("/api/markets/favorites/NOPE123")).status_code == 404
    # a viewer has their own (and may change them)
    r = await v.put("/api/markets/favorites/NVDA")
    assert r.status_code == 200 and [i["symbol"] for i in r.json()["items"]] == ["NVDA"]
    assert len((await c.get("/api/markets/favorites")).json()["items"]) == 3
    r = await c.put("/api/markets/favorites", json={"symbols": ["ENI.MI", "AAPL", "MSFT"]})
    assert [i["symbol"] for i in r.json()["items"]] == ["ENI.MI", "AAPL", "MSFT"]
    r = await c.put("/api/markets/favorites", json={"symbols": ["AAPL"]})
    assert r.status_code == 400
    r = await c.delete("/api/markets/favorites/AAPL")
    assert [i["symbol"] for i in r.json()["items"]] == ["ENI.MI", "MSFT"]
    # apart from the agents' watchlists
    from smtrading.watch.models import WatchlistItem

    async with portal.sm() as s:
        assert await s.scalar(select(func.count()).select_from(WatchlistItem)) == 0


# ------------------------------------------------------------------ screener


def _daily(closes, end: date, volume_last=100):
    out = []
    for n, c in enumerate(closes):
        day = end - timedelta(days=len(closes) - 1 - n)
        c = D(str(c))
        out.append(Bar(datetime(day.year, day.month, day.day, tzinfo=UTC), c, c, c - 1, c,
                       D(volume_last if n == len(closes) - 1 else 100)))
    return out


async def test_screener_job_idempotent_and_gated(portal):
    from smtrading.instruments.models import AssetClass, Instrument
    from smtrading.marketsportal.models import MarketScreenRow
    from smtrading.marketsportal.screener import JOB, ScreenerJob
    from smtrading.risk.models import JobRun

    async with portal.sm() as s:
        cryptos = list(await s.scalars(select(Instrument.symbol).where(
            Instrument.asset_class == AssetClass.crypto, Instrument.active.is_(True))
            .order_by(Instrument.symbol)))
    now = datetime(2026, 11, 22, 1, tzinfo=UTC)  # Sunday 01:00: only crypto is due
    falling = [200] * 250 + [200 - 5 * n for n in range(1, 21)]
    for sym in cryptos:
        portal.qs.set_bars(sym, _daily(falling, date(2026, 11, 21), volume_last=500))
    open_gate = {"v": False}

    async def gate():
        return open_gate["v"]

    job = ScreenerJob(portal.sm, portal.qs, clock=lambda: now, chunk=40, gate=gate)
    # the first ever run of every group is due at once; busy providers: nothing asked
    assert await job.tick() == 0
    open_gate["v"] = True
    total, ticks = 0, 0
    while await job._due(now) and ticks < 500:
        total += await job.tick()
        ticks += 1
    async with portal.sm() as s:
        rows = {r.symbol: r for r in await s.scalars(select(MarketScreenRow))}
        done = set(await s.scalars(select(JobRun.key).where(JobRun.job == JOB)))
    assert set(cryptos) <= set(rows) and total == len(rows)
    assert "2026-11-22:crypto" in done
    # equities have no bars in the fake: given up after MAX_ATTEMPTS, then the day is done
    assert "2026-11-22:us" in done and "2026-11-22:eu" in done
    row = rows[cryptos[0]]
    assert row.as_of == date(2026, 11, 21) and row.rsi14 < 30
    # done for the day: another tick asks nothing
    assert await job.tick() == 0

    r = await portal.client.get("/api/markets/screener",
                                params={"preset": "volume_spike", "universe": "CRYPTO"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == len(cryptos) and body["computed"] == len(rows)
    item = body["items"][0]
    assert D(item["volume_ratio"]) == 5 and D(item["vs_sma200_pct"]) < 0
    r = await portal.client.get("/api/markets/screener", params={"preset": "rsi_overbought"})
    assert r.json()["matched"] == 0 and r.json()["total"] > len(cryptos)
    assert (await portal.client.get("/api/markets/screener",
                                    params={"preset": "x"})).status_code == 400

    # multi-period sector map from the same rows
    r = await portal.client.get("/api/markets/snapshot",
                                params={"universe": "CRYPTO", "period": "1m"})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["period"] == "1m" and snap["covered"] == len(cryptos)
    assert D(snap["sectors"][0]["avg_change_pct"]) < 0 and snap["losers"]
    assert (await portal.client.get("/api/markets/snapshot",
                                    params={"period": "5y"})).status_code == 400


# ------------------------------------------------------------------ fundamentals, calendar


async def test_fundamentals_cache_and_fallback(portal):
    ts = int(datetime.now(UTC).timestamp()) + 5 * 86400
    portal.raw.rows["AAPL"] = {"marketCap": 4e12, "trailingPE": 38.1, "forwardPE": 32.4,
                               "dividendYield": 0.31, "fiftyTwoWeekLow": 243.42,
                               "fiftyTwoWeekHigh": 360.83, "beta": 1.09,
                               "earningsTimestampStart": ts, "currency": "USD"}
    r = await portal.client.get("/api/instruments/AAPL/fundamentals")
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["pe_trailing"] == "38.1" and f["beta"] == "1.09" and f["source"] == "yahoo"
    assert f["next_earnings"] == (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    n = len(portal.raw.calls)
    await portal.client.get("/api/instruments/AAPL/fundamentals")
    assert len(portal.raw.calls) == n  # 12 h cache
    assert 11 * 3600 < await portal.redis.ttl("portal:fund:v1:AAPL") <= 12 * 3600
    # not known to Yahoo: the router's fundamentals
    portal.qs.set_fundamentals("MSFT", Fundamentals(
        "MSFT", "Microsoft", None, None, D("3e12"), D("35"), None, D("0.7"), D(500), D(350),
        "USD", {"beta": "0.9"}))
    f = (await portal.client.get("/api/instruments/MSFT/fundamentals")).json()
    assert f["source"] == "router" and f["pe_trailing"] == "35" and f["beta"] == "0.9"
    # nowhere: every figure null
    f = (await portal.client.get("/api/instruments/NVDA/fundamentals")).json()
    assert f["source"] is None and f["market_cap"] is None


async def test_calendar_week(portal):
    from smtrading.portfolios.service import create_portfolio
    from smtrading.positions.models import Position

    today = datetime.now(UTC).date()
    soon = datetime.combine(today, datetime.min.time(), UTC) + timedelta(hours=14)
    portal.raw.rows["AAPL"] = {"earningsTimestamp": int(soon.timestamp())}
    portal.raw.rows["ENI.MI"] = {"exDividendDate": int(soon.timestamp())}
    async with portal.sm() as s:
        p = await create_portfolio(s, name="Portafoglio eToro", initial_cash=D(1000))
        s.add(Position(portfolio_id=p.id, symbol="ENI.MI", qty=D(5), avg_price=D(15)))
        await s.commit()
    r = await portal.client.get("/api/markets/calendar")
    assert r.status_code == 200, r.text
    body = r.json()
    events = {(e["kind"], e["symbol"]): e for e in body["events"] if e["symbol"]}
    assert ("earnings", "AAPL") in events and not events[("earnings", "AAPL")]["held"]
    eni = events[("ex_dividend", "ENI.MI")]
    assert eni["held"] and eni["portfolios"] == ["Portafoglio eToro"]
    # covered: only the symbols a source answered for (the fake knows two); the others
    # are cached empty, so the next request asks Yahoo nothing
    assert body["total"] > 500 and body["covered"] == 2
    assert 0 < await portal.redis.ttl("portal:calendar:v1:" + today.isoformat()) <= 300
    assert body["macro"]["as_of"] == "2026-11-21"
    # batched: at most 50 symbols per Yahoo request
    quotes = [c for c in portal.raw.calls if c.startswith("quote:")]
    assert all(len(c.split(",")) <= 50 for c in quotes)
    n = len(portal.raw.calls)
    await portal.client.get("/api/markets/calendar")
    assert len(portal.raw.calls) == n


async def test_news_filters(portal):
    from smtrading.portfolios.service import create_portfolio
    from smtrading.positions.models import Position

    now = datetime.now(UTC)
    portal.qs.set_news([
        NewsItem("n1", now - timedelta(hours=1), "Wall Street giù", None, "https://x/1",
                 "Reuters", ("SPY",)),
        NewsItem("n2", now - timedelta(hours=2), "Apple trimestrale", None, None, "AP",
                 ("AAPL",)),
        NewsItem("n3", now - timedelta(days=5), "Vecchia", None, None, "AP", ("SPY",)),
    ])
    body = (await portal.client.get("/api/markets/news")).json()
    assert body["symbols"][:2] == ["SPY", "QQQ"] and [i["id"] for i in body["items"]] == ["n1"]
    body = (await portal.client.get("/api/markets/news", params={"symbol": "aapl"})).json()
    assert [i["id"] for i in body["items"]] == ["n2"]
    assert (await portal.client.get("/api/markets/news",
                                    params={"portfolios": "true"})).json()["items"] == []
    async with portal.sm() as s:
        p = await create_portfolio(s, name="P", initial_cash=D(1000))
        s.add(Position(portfolio_id=p.id, symbol="AAPL", qty=D(1), avg_price=D(1)))
        await s.commit()
    await portal.redis.delete(*[k async for k in portal.redis.scan_iter("portal:news:*")])
    body = (await portal.client.get("/api/markets/news", params={"portfolios": "true"})).json()
    assert body["symbols"] == ["AAPL"] and body["items"][0]["id"] == "n2"
    body = (await portal.client.get("/api/markets/news",
                                    params={"sector": "Tecnologia"})).json()
    assert "AAPL" == body["symbols"][0]  # held first


# ------------------------------------------------------------------ personal alerts


async def test_personal_alerts_fire_once(portal):
    from smtrading.marketsportal.alerts import CATEGORY, PersonalAlerts
    from smtrading.telegram.models import TelegramOutbox
    from smtrading.watch.models import Alert

    c = portal.client
    r = await c.post("/api/markets/price-alerts", json={
        "symbol": "aapl", "direction": "above", "threshold": "200", "note": "breakout"})
    assert r.status_code == 200, r.text
    a1 = r.json()
    assert a1["symbol"] == "AAPL" and a1["status"] == "active"
    r = await c.post("/api/markets/price-alerts", json={
        "symbol": "SPX", "source": "overview", "direction": "below", "threshold": 120})
    a2 = r.json()
    assert a2["label"] == "S&P 500"
    for bad in ({"symbol": "AAPL", "direction": "up", "threshold": 1},
                {"symbol": "AAPL", "direction": "above", "threshold": "-1"},
                {"symbol": "XXX", "source": "overview", "direction": "above", "threshold": 1},
                {"symbol": "AAPL", "source": "x", "direction": "above", "threshold": 1}):
        assert (await c.post("/api/markets/price-alerts", json=bad)).status_code == 400
    # owner only: a viewer neither reads nor writes them
    assert (await portal.viewer.get("/api/markets/price-alerts")).status_code == 403
    assert (await portal.viewer.post("/api/markets/price-alerts", json={
        "symbol": "AAPL", "direction": "above", "threshold": 1})).status_code == 403

    overview = OverviewService(portal.redis, PortalYahoo(portal.redis, None, raw=portal.raw),
                               clock=lambda: SAT)
    job = PersonalAlerts(portal.sm, portal.qs, portal.redis, overview)
    portal.qs.set_day_change("AAPL", D("199"), D("190"))
    assert await job.tick() == 1  # SPX at 110 < 120
    portal.qs.set_day_change("AAPL", D("201"), D("190"))
    assert await job.tick() == 1
    assert await job.tick() == 0  # once
    async with portal.sm() as s:
        msgs = list(await s.scalars(select(TelegramOutbox).where(
            TelegramOutbox.category == CATEGORY).order_by(TelegramOutbox.id)))
        assert await s.scalar(select(func.count()).select_from(Alert)) == 0  # not agents'
    assert [m.text for m in msgs] == [
        "📉 Avviso di prezzo: S&P 500 sotto 120 (ora 110)",
        "📈 Avviso di prezzo: AAPL sopra 200 (ora 201)\nNota: breakout",
    ]
    rows = {a["id"]: a for a in (await c.get("/api/markets/price-alerts")).json()}
    assert rows[a1["id"]]["status"] == "triggered" and rows[a1["id"]]["triggered_price"] == "201"
    # edit re-arms; delete removes
    r = await c.patch(f"/api/markets/price-alerts/{a1['id']}", json={"threshold": "250"})
    assert r.json()["status"] == "active" and r.json()["triggered_at"] is None
    assert await job.tick() == 0
    assert (await c.delete(f"/api/markets/price-alerts/{a1['id']}")).status_code == 200
    assert (await c.delete(f"/api/markets/price-alerts/{a1['id']}")).status_code == 404
    # a fresh cached quote wins over the day change
    from smtrading.marketdata.cache import write_quote
    from smtrading.marketdata.types import Quote

    r = await c.post("/api/markets/price-alerts", json={
        "symbol": "MSFT", "direction": "below", "threshold": "300"})
    await write_quote(portal.redis, Quote("MSFT", None, None, D("299"), datetime.now(UTC), "x"))
    portal.qs.set_day_change("MSFT", D("400"), D("390"))
    assert await job.tick() == 1


async def test_markets_v2_not_in_mcp(mcp_server, mcp_list_tools):
    """The agents' MCP server lists no tool of the portal's market pages."""
    tools = await mcp_list_tools(mcp_server.url, mcp_server.token)
    assert tools
    for t in tools:
        assert not any(w in t for w in ("favorite", "favourite", "preferit", "screener_tec",
                                        "price_alert", "overview"))
