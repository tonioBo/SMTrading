"""Short-bar history (intraday plan Task 14): Alpaca paging, the bars cache, the backtest span
limit and the bars the watch fetches. No network: respx on Alpaca, a fake provider, a fake router."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
import redis.asyncio as aioredis
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.agents.service import create_agent
from smtrading.backtest.runner import enqueue_backtest
from smtrading.backtest.strategies import min_bars, validate_params
from smtrading.common.db import make_engine
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.connectors.models import Connector, ProviderPriority
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.marketdata.provider import DataProvider
from smtrading.marketdata.providers.alpaca import AlpacaProvider
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.router import MarketDataRouter
from smtrading.marketdata.types import Bar, Capability, Timeframe
from smtrading.seed import run_seed

API = "https://data.alpaca.markets"
BARS_URL = f"{API}/v2/stocks/bars"
D = Decimal


def _raw(ts: datetime, i: int) -> dict:
    return {"t": ts.isoformat().replace("+00:00", "Z"), "o": 100 + i, "h": 101 + i,
            "l": 99 + i, "c": 100.5 + i, "v": 1000 + i, "n": 5, "vw": 100}


def _page(stamps: list[datetime], first: int, token: str | None) -> httpx.Response:
    return httpx.Response(200, json={
        "bars": {"AAPL": [_raw(t, first + k) for k, t in enumerate(stamps)]},
        "next_page_token": token,
    })


@pytest.fixture
async def alpaca():
    async with httpx.AsyncClient() as http:
        yield AlpacaProvider({"key_id": "KID", "secret_key": "SEC"}, http)


@respx.mock
async def test_alpaca_15m_pages_are_joined(alpaca):
    t0 = datetime(2026, 1, 14, 14, 30, tzinfo=UTC)
    stamps = [t0 + timedelta(minutes=15 * i) for i in range(12)]
    route = respx.get(BARS_URL).mock(side_effect=[
        _page(stamps[:4], 0, "TOK1"), _page(stamps[4:8], 4, "TOK2"), _page(stamps[8:], 8, None),
    ])
    bars = await alpaca.get_bars("AAPL", "AAPL", Timeframe.m15, t0, t0 + timedelta(hours=3))
    assert [b.ts for b in bars] == stamps
    assert [b.close for b in bars] == [D("100.5") + i for i in range(12)]
    params = [c.request.url.params for c in route.calls]
    assert len(params) == 3
    assert all(p["timeframe"] == "15Min" and p["feed"] == "iex" for p in params)
    assert "page_token" not in params[0]
    assert [p["page_token"] for p in params[1:]] == ["TOK1", "TOK2"]


@respx.mock
async def test_alpaca_1m_session_in_one_call(alpaca):
    open_ = datetime(2026, 1, 14, 14, 30, tzinfo=UTC)  # 09:30 New York
    stamps = [open_ + timedelta(minutes=i) for i in range(390)]
    route = respx.get(BARS_URL).mock(side_effect=[
        _page(stamps[:200], 0, "TOK1"), _page(stamps[200:], 200, None),
    ])
    bars = await alpaca.get_bars("AAPL", "AAPL", Timeframe.m1, open_, open_ + timedelta(hours=6.5))
    assert len(bars) == 390
    assert [b.ts for b in bars] == stamps
    assert bars[-1].ts == datetime(2026, 1, 14, 20, 59, tzinfo=UTC)
    assert all(c.request.url.params["timeframe"] == "1Min" for c in route.calls)
    assert len(route.calls) == 2


# ---------------------------------------------------------------- router cache


class _Fake(DataProvider):
    name = "fake_a"
    capabilities = frozenset({Capability.bars})
    asset_classes = frozenset({AssetClass.crypto})
    calls: ClassVar[list] = []

    async def get_bars(self, symbol, provider_symbol, timeframe, start, end):
        type(self).calls.append((timeframe, start, end))
        step = timedelta(minutes=15)
        out, ts = [], start
        while ts < end:
            out.append(Bar(ts, D(1), D(2), D("0.5"), D("1.5"), D(10)))
            ts += step
        return out

    def _default_symbol(self, symbol, asset_class):
        return symbol.replace("-", "")


@pytest.fixture
async def router(fresh_db, redis_url, monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setitem(PROVIDERS, "fake_a", _Fake)
    monkeypatch.setattr(_Fake, "calls", [])
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        (await s.get(Connector, "rssnews")).enabled = False
        s.add_all([
            Connector(name="fake_a", enabled=True),
            ProviderPriority(asset_class=AssetClass.crypto, capability=Capability.bars,
                             providers=["fake_a"]),
            Instrument(symbol="BTC-USD", name="BTC-USD", asset_class=AssetClass.crypto,
                       market="crypto", currency="USD", provider_symbols={}),
        ])
        await s.commit()
    redis = aioredis.from_url(redis_url, decode_responses=True)
    await redis.flushdb()
    async with httpx.AsyncClient() as http:
        yield MarketDataRouter(sm, redis, http)
    await redis.aclose()
    await engine.dispose()


async def test_intraday_bars_cached_until_next_close(router):
    q = datetime(2026, 1, 14, 15, 0, tzinfo=UTC)  # a quarter-hour boundary
    start = q - timedelta(hours=5)
    first = await router.get_bars("BTC-USD", Timeframe.m15, start, q + timedelta(minutes=2))
    again = await router.get_bars("BTC-USD", Timeframe.m15, start, q + timedelta(minutes=13))
    assert again == first and len(_Fake.calls) == 1  # same quarter hour: one request
    await router.get_bars("BTC-USD", Timeframe.m15, start, q + timedelta(minutes=16))
    assert len(_Fake.calls) == 2  # the next bar closed: the key (floored end) changed
    assert _Fake.calls[1][2] == q + timedelta(minutes=15)


# ---------------------------------------------------------------- backtest span


@pytest.fixture
async def maker(fresh_db):
    engine = make_engine(fresh_db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await run_seed(s)
        await s.commit()
    yield sm
    await engine.dispose()


async def test_backtest_15m_60_days_accepted(maker):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    async with maker() as s:
        agent, _ = await create_agent(s, "a")
        await s.commit()
    args = {"strategy": "buy_and_hold", "symbols": ["AAPL"], "timeframe": "15m", "start": t0,
            "initial_cash": D(10000), "params": {}, "portfolio_id": None}
    async with maker() as s:
        bt = await enqueue_backtest(s, agent.id, end=t0 + timedelta(days=60), **args)
        await s.commit()
    assert bt.id is not None
    async with maker() as s:
        with pytest.raises(SMTError) as e:
            await enqueue_backtest(s, agent.id, end=t0 + timedelta(days=91), **args)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    assert e.value.details["limit_days"] == 90 and e.value.details["timeframe"] == "15m"


# ---------------------------------------------------------------- the watch fetch


class _SessionRouter:
    """Stands in for the router: 15m bars of the NYSE regular session (weekdays, no holiday),
    those closed by `end`, inside [start, end]."""

    HOLIDAYS = frozenset({datetime(2026, 1, 19).date()})  # Martin Luther King day

    async def get_bars(self, symbol, tf, start, end):
        out = []
        day = start.date()
        while day <= end.date():
            if day.weekday() < 5 and day not in self.HOLIDAYS:
                open_ = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)  # winter
                for i in range(26):
                    ts = open_ + timedelta(minutes=15 * i)
                    if start <= ts and ts + timedelta(minutes=15) <= end:
                        out.append(Bar(ts, D(1), D(2), D("0.5"), D("1.5"), D(10)))
            day += timedelta(days=1)
        return out


async def test_watch_fetch_has_enough_bars():
    need = min_bars("rsi_reversion", validate_params("rsi_reversion", {"period": 14}))
    assert need >= 15
    now = datetime(2026, 1, 20, 15, 0, tzinfo=UTC)  # Tuesday, after a weekend and a holiday
    bars = await fetch_bars(_SessionRouter(), "AAPL", Timeframe.m15, 250, now=now)
    assert len(bars) >= need
    assert [b.ts for b in bars] == sorted(b.ts for b in bars)
    assert bars[-1].ts == now - timedelta(minutes=15)
