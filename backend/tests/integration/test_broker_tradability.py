"""Plan 44: what the broker of a linked portfolio lets SMTrading trade.

Adapters (eToro search + eligibility, Alpaca assets, Trading 212 instrument list) on the
FakeLiveBroker, whose shapes are those read on the demo / paper accounts (23/11/2026);
the Redis cache; the refusal at placement and in the preview; the worker refresh; the
check_tradable MCP tool. Never reaches the network.
"""

import time
import uuid
from decimal import Decimal

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.brokers import tradability as tr
from smtrading.brokers.alpaca_paper import AlpacaPaperAdapter
from smtrading.brokers.etoro_demo import EtoroDemoAdapter
from smtrading.brokers.live.fake import FakeLiveBroker
from smtrading.brokers.service import create_link, link_portfolio
from smtrading.brokers.t212_demo import T212DemoAdapter
from smtrading.brokers.tradable import Tradability
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.engine.quotesource import FakeQuoteSource
from smtrading.engine.service import PlaceOrderRequest, place_order, preview_order
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.portfolios.service import create_portfolio
from smtrading.seed import run_seed

D = Decimal
ETORO_KEYS = {"api_key": "CANARYAPIKEY0001", "user_key": "CANARYUSERKEY-0002"}
ALPACA_KEYS = {"key_id": "KID-SECRET", "secret_key": "SEC-SECRET"}


def _inst(symbol, ac=AssetClass.us_equity, cur="USD"):
    return Instrument(symbol=symbol, name=symbol, asset_class=ac, market="X", currency=cur,
                      provider_symbols={}, shortable=True, lot_size=D("0.000001"))


@pytest.fixture
def fake():
    f = FakeLiveBroker(price=D(100))
    f.etoro["demo"].credit = D(100000)
    return f


@pytest.fixture
async def http(fake):
    async with httpx.AsyncClient(transport=fake.transport()) as c:
        yield c


# --- adapters --------------------------------------------------------------------------


async def test_etoro_reads_found_unleveraged_cfd_only_and_leverage_only(http):
    a = EtoroDemoAdapter(dict(ETORO_KEYS), http)
    got = await a.tradability([
        _inst("AAPL"), _inst("SPY"), _inst("NSDQ100"), _inst("DIA"),
        _inst("ENEL.MI", AssetClass.eu_equity, "EUR"), _inst("EUNH.DE", AssetClass.eu_equity),
        _inst("EURUSD", AssetClass.forex)])
    assert got["AAPL"] == Tradability(True, True, False, True, True, None)  # W8Ben, real
    assert got["DIA"].tradable and got["ENEL.MI"].tradable
    assert got["SPY"] == Tradability(False, False, False, True, True, "leverage_only")
    assert got["SPY"].reason == "solo con leva presso il broker"
    assert got["NSDQ100"].code == "leverage_only"
    assert got["NSDQ100"].reason == "solo con leva presso il broker"
    assert got["EUNH.DE"].code == "not_found"
    assert got["EUNH.DE"].reason == "non disponibile presso il broker"
    assert got["EURUSD"].code == "asset_class" and got["EURUSD"].tradable is False
    # the 24 h cache of the adapter agrees: a buy of SPY is refused before any send
    assert a._entry(_inst("SPY"))["eligible"] is False


async def test_etoro_reads_again_and_unknown_when_the_broker_fails(http, fake):
    a = EtoroDemoAdapter(dict(ETORO_KEYS), http)
    await a.tradability([_inst("AAPL")])
    searches = sum(1 for m, p in fake.calls if p.endswith("/search"))
    await a.tradability([_inst("AAPL")])  # fresh answers: not the 24 h cache
    assert sum(1 for m, p in fake.calls if p.endswith("/search")) == searches + 1

    def down(request):
        return httpx.Response(503, json={"errorMessage": "down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as bad:
        with pytest.raises(RuntimeError):
            await EtoroDemoAdapter(dict(ETORO_KEYS), bad).tradability([_inst("MSFT")])


async def test_alpaca_reads_assets(http, fake):
    fake.alpaca_assets.update({"HALT": {"tradable": False, "fractionable": False,
                                        "shortable": False}, "ZZZZQX": None})
    a = AlpacaPaperAdapter(dict(ALPACA_KEYS), http)
    got = await a.tradability([
        _inst("SPY"), _inst("HALT"), _inst("ZZZZQX"), _inst("BTC-USD", AssetClass.crypto),
        _inst("EUNL.DE", AssetClass.eu_equity, "EUR")])
    assert got["SPY"] == Tradability(True, True, True, True, True, None)
    assert got["HALT"].code == "not_tradable" and got["HALT"].tradable is False
    assert got["ZZZZQX"].code == "not_found"
    assert got["BTC-USD"].tradable and got["BTC-USD"].short is False
    assert got["EUNL.DE"].code == "asset_class"
    assert ("GET", "/v2/assets/BTC/USD") in fake.calls


async def test_t212_reads_the_instrument_list(http):
    a = T212DemoAdapter({"api_key": "K", "api_secret": "S"}, http)
    got = await a.tradability([_inst("AAPL"), _inst("NVDA")])
    assert got["AAPL"].tradable is True and got["AAPL"].short is False
    assert got["NVDA"].code == "not_found"


# --- service, cache, placement -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch, pg_url, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", pg_url)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


@pytest.fixture
async def redis(redis_url):
    r = Redis.from_url(redis_url)
    await r.flushdb()
    yield r
    await r.aclose()


async def _linked(maker, kind="etoro_demo", keys=ETORO_KEYS) -> tuple[uuid.UUID, uuid.UUID]:
    async with maker() as s:
        p = await create_portfolio(s, name=f"P-{uuid.uuid4().hex[:8]}",
                                   initial_cash=D(100000), leverage=D(1))
        link = await create_link(s, f"L-{uuid.uuid4().hex[:8]}", kind, dict(keys))
        await link_portfolio(s, p.id, link.id)
        await s.commit()
        return p.id, link.id


async def test_portfolio_check_caches_and_answers_the_catalog(maker, redis, http, fake):
    pid, link_id = await _linked(maker)
    async with maker() as s:
        got = await tr.portfolio_tradability(s, maker, redis, pid,
                                             ["spy", "AAPL", "NOPE-XYZ", "EURUSD"], http=http)
    assert list(got) == ["SPY", "AAPL", "NOPE-XYZ", "EURUSD"]
    assert got["SPY"].code == "leverage_only" and got["AAPL"].tradable is True
    assert got["NOPE-XYZ"].code == "not_in_catalog"
    assert got["NOPE-XYZ"].reason == "strumento non trovato"
    assert got["EURUSD"].code == "asset_class"
    calls = len(fake.calls)
    async with maker() as s:  # cached: no request
        again = await tr.portfolio_tradability(s, maker, redis, pid, ["SPY", "AAPL"], http=http)
    assert len(fake.calls) == calls and again["SPY"] == got["SPY"]
    cached = await tr.read_cached(redis, link_id, ["SPY"], now=time.time() + tr.FRESH_S + 1)
    assert cached == {}  # stale after 12 h

    async with maker() as s:  # a portfolio without a broker: the catalog
        p = await create_portfolio(s, name="Sim", initial_cash=D(1000))
        await s.commit()
        sim = await tr.portfolio_tradability(s, maker, redis, p.id, ["SPY", "EURUSD"])
    assert sim["SPY"] == Tradability(True, True, True, True, True, None)
    assert sim["EURUSD"].tradable is True and sim["EURUSD"].fractional is False


async def test_unknown_is_not_cached(maker, redis):
    pid, link_id = await _linked(maker)

    def down(request):
        return httpx.Response(503, json={"errorMessage": "down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as bad, maker() as s:
        got = await tr.portfolio_tradability(s, maker, redis, pid, ["SPY"], http=bad)
    assert got["SPY"].tradable is None
    assert got["SPY"].reason == "verifica presso il broker non riuscita: riprova più tardi"
    assert await tr.read_cached(redis, link_id, ["SPY"]) == {}


def _req(pid, symbol="SPY", side=OrderSide.buy, **kw):
    return PlaceOrderRequest(portfolio_id=pid, symbol=symbol, side=side,
                             type=kw.pop("type", OrderType.market), tif=TimeInForce.gtc,
                             qty=kw.pop("qty", D(1)), source="agent", **kw)


async def test_placement_refuses_at_once_what_the_cache_says_the_broker_refuses(
        maker, redis, http):
    pid, _ = await _linked(maker)
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    for sym in ("SPY", "AAPL"):
        qs.set_quote(sym, D("99.90"), D("100.00"), D("99.95"))
    async with maker() as s:  # nothing cached yet: accepted (BrokerSync decides)
        order = await place_order(s, qs, _req(pid, type=OrderType.limit,
                                              limit_price=D(90)), redis=redis)
        assert order.status != OrderStatus.rejected
        await s.rollback()
        await tr.portfolio_tradability(s, maker, redis, pid, ["SPY", "AAPL"], http=http)
    async with maker() as s:
        check = await preview_order(s, qs, _req(pid), redis=redis)
        assert (check.ok, check.code, check.message) == (
            False, ErrorCode.INVALID_ORDER, "solo con leva presso il broker")
        with pytest.raises(SMTError) as e:
            await place_order(s, qs, _req(pid), redis=redis)
        assert e.value.code == ErrorCode.INVALID_ORDER
        assert e.value.message == "solo con leva presso il broker"
        await s.commit()  # the rejection is stored
        ok = await place_order(s, qs, _req(pid, "AAPL"), redis=redis)
        assert ok.status != OrderStatus.rejected and ok.broker_link_id is not None
        # a sell is never refused for it (a position elsewhere is closed as usual)
        sell = await preview_order(s, qs, _req(pid, side=OrderSide.sell, reduce_only=True),
                                   redis=redis)
        assert sell.message != "solo con leva presso il broker"


async def test_bracket_entry_refused_at_once(maker, redis, http):
    from smtrading.engine.service import place_bracket_order

    pid, _ = await _linked(maker)
    qs = FakeQuoteSource()
    qs.set_fx("EUR", "USD", D("1.25"))
    qs.set_quote("SPY", D("99.90"), D("100.00"), D("99.95"))
    async with maker() as s:
        await tr.portfolio_tradability(s, maker, redis, pid, ["SPY"], http=http)
        with pytest.raises(SMTError) as e:
            await place_bracket_order(s, qs, _req(pid), take_profit=D(120),
                                      stop_loss=D(80), redis=redis)
    assert e.value.code == ErrorCode.INVALID_ORDER
    assert e.value.message == "solo con leva presso il broker"


async def test_refresher_reads_what_matters_first_in_chunks(maker, redis, http, fake):
    from smtrading.engine.models import Order

    pid, link_id = await _linked(maker)
    async with maker() as s:  # an open order on SPY: it matters
        s.add(Order(id=uuid.uuid4(), portfolio_id=pid, symbol="SPY", side=OrderSide.buy,
                    type=OrderType.limit, tif=TimeInForce.gtc, qty=D(1), limit_price=D(1),
                    filled_qty=D(0), status=OrderStatus.pending, source="agent"))
        await s.commit()
    job = tr.TradabilityRefresher(maker, redis, http, chunk=3)
    assert await job.tick() >= 1
    first = await tr.read_cached(redis, link_id, ["SPY"])
    assert first["SPY"].code == "leverage_only"
    assert len(await tr.cached_at(redis, link_id)) == 3
    await job.tick()
    assert len(await tr.cached_at(redis, link_id)) == 6


# --- MCP ---------------------------------------------------------------------------------


async def test_mcp_check_tradable_preview_and_place(mcp_server, mcp_call, mcp_list_tools,
                                                    monkeypatch, fake):
    from smtrading.brokers.models import BrokerLink

    monkeypatch.setattr(tr, "http_client",
                        lambda: httpx.AsyncClient(transport=fake.transport()))
    qs = mcp_server.qs
    qs.set_fx("EUR", "USD", D("1.25"))
    for sym in ("SPY", "DIA"):
        qs.set_quote(sym, D("99.90"), D("100.00"), D("99.95"))
    engine = make_engine(mcp_server.db_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        await run_seed(s)
        sim = await create_portfolio(s, name="Libero", initial_cash=D(1000))
        from smtrading.agents import service as agents

        await agents.assign_portfolio(s, mcp_server.agent_id, sim.id, "read")
        link = await create_link(s, "Conto", "etoro_demo", dict(ETORO_KEYS))
        await s.commit()
        await link_portfolio(s, mcp_server.demo_id, link.id)
        await s.commit()
        assert await s.get(BrokerLink, link.id) is not None
    await engine.dispose()

    async def call(tool, args):
        return await mcp_call(mcp_server.url, mcp_server.token, tool, args)

    assert "check_tradable" in await mcp_list_tools(mcp_server.url, mcp_server.token)
    pid = str(mcp_server.demo_id)
    is_error, got = await call("check_tradable", {"portfolio_id": pid,
                                                  "symbols": ["SPY", "dia", "NOPE-XYZ"]})
    assert not is_error, got
    assert got["SPY"] == {"tradable": False, "long_unleveraged": False, "short": False,
                          "fractional": True, "partial_close": True,
                          "reason": "solo con leva presso il broker"}
    assert got["DIA"]["tradable"] is True and got["DIA"]["reason"] is None
    assert got["NOPE-XYZ"] == {"tradable": False, "long_unleveraged": False, "short": False,
                               "fractional": None, "partial_close": None,
                               "reason": "strumento non trovato"}
    is_error, free = await call("check_tradable", {"portfolio_id": str(sim.id),
                                                   "symbols": ["SPY"]})
    assert not is_error and free["SPY"]["tradable"] is True, free
    is_error, bad = await call("check_tradable", {"portfolio_id": pid, "symbols": []})
    assert is_error and bad["code"] == "INVALID_ORDER"

    order = {"portfolio_id": pid, "symbol": "SPY", "side": "buy", "type": "market",
             "qty": "1"}
    is_error, preview = await call("preview_order", order)
    assert not is_error, preview
    assert preview["ok"] is False and preview["code"] == "INVALID_ORDER"
    assert preview["tradable"]["reason"] == "solo con leva presso il broker"
    is_error, err = await call("place_order", order)
    assert is_error and err["code"] == "INVALID_ORDER", err
    assert err["message"] == "solo con leva presso il broker"
    is_error, ok = await call("preview_order", {**order, "symbol": "DIA"})
    assert not is_error and ok["tradable"]["tradable"] is True, ok
