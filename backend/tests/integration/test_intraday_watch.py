"""Intraday signals: `strategy_signals` with a selector (the golden pass-through) and the
false -> true trigger detection at a bar close (`intraday.watch`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.activity.models import Decision
from smtrading.agents import service as agents
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.quotesource import FakeMarketData
from smtrading.instruments.market_hours import set_closures
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.intraday.watch import STATE_KEY, STATE_TTL_S, evaluate_close, intraday_selector
from smtrading.marketdata.types import Bar, Timeframe
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.signals.service import strategy_signals

D = Decimal
BAR_CLOSE = datetime(2026, 12, 1, 16, 0, tzinfo=UTC)  # Tuesday, NYSE open
NOW = BAR_CLOSE + timedelta(minutes=1)
STEP = timedelta(minutes=15)
PARAMS = {"period": 2, "buy_below": 10, "sell_above": 70}
FALLING = [200 - i for i in range(40)]  # RSI(2) = 0: the entry fires
RISING = [100 + i for i in range(40)]  # RSI(2) = 100: the exit fires, no entry


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


def _bars(closes: list[int], last_close: datetime = BAR_CLOSE) -> list[Bar]:
    n = len(closes)
    # a bar is stamped with its open time
    return [Bar(last_close - STEP * (n - i), D(c), D(c + 1), D(c - 1), D(c), D(1))
            for i, c in enumerate(closes)]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key):
        v = self.data.get(key)
        return None if v is None else v.encode()

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex


class Env:
    def __init__(self, session):
        self.s = session
        self.qs = FakeMarketData()
        self.redis = FakeRedis()

    async def setup(self):
        self.ceo, _ = await agents.create_agent(self.s, "CEO intraday-watch")
        self.p1 = Portfolio(name="Giorno A", intraday_enabled=True)
        self.p2 = Portfolio(name="Giorno B", intraday_enabled=True)
        self.plain = Portfolio(name="Solo giornaliero")
        self.hourly = Portfolio(name="Solo orario", intraday_enabled=True,
                                intraday_timeframes=["1h"])
        self.old = Portfolio(name="Archivio", intraday_enabled=True,
                             status=PortfolioStatus.archived)
        self.s.add_all([self.p1, self.p2, self.plain, self.hourly, self.old])
        for sym, ac, market, ccy in (("WSPY", AssetClass.us_equity, "NYSE", "USD"),
                                     ("WQQQ", AssetClass.us_equity, "NYSE", "USD"),
                                     ("WENEL.MI", AssetClass.eu_equity, "MTA", "EUR")):
            self.s.add(Instrument(symbol=sym, name=sym, asset_class=ac, market=market,
                                  currency=ccy))
        await self.s.flush()

    async def strategy(self, ref, symbols, portfolios, timeframe="15m", params=PARAMS):
        self.s.add(Decision(
            agent_id=self.ceo.id, kind="strategy", summary=f"Strategia {ref}",
            details={"ref": ref, "status": "approvata", "strategy": "rsi_reversion",
                     "timeframe": timeframe, "params": params,
                     "portfolios": [str(p.id) for p in portfolios]},
            symbols=list(symbols), portfolio_id=None))
        await self.s.flush()

    def bars(self, symbol, closes):
        self.qs.set_bars(symbol, _bars(closes))

    def prev(self, portfolio, ref, symbol, rule, value):
        self.redis.data[STATE_KEY.format(portfolio_id=portfolio.id, ref=ref, symbol=symbol,
                                         rule=rule)] = value

    async def run(self, **kw):
        return await evaluate_close(self.s, self.qs, self.redis, Timeframe.m15, "NYSE",
                                    kw.pop("bar_close", BAR_CLOSE), kw.pop("now", NOW), **kw)


@pytest.fixture
async def env(db_session):
    e = Env(db_session)
    await e.setup()
    return e


async def test_select_passthrough_matches_golden(env):
    await env.strategy("W-1", ["WSPY", "WQQQ"], [env.p1])
    env.bars("WSPY", RISING)
    env.bars("WQQQ", FALLING)
    for select in (None, lambda a: a):
        kw = {} if select is None else {"select": select}
        [s] = await strategy_signals(env.s, env.qs, now=NOW, **kw)
        assert s.status == "ok" and s.timeframe == "15m"
        assert [x.symbol for x in s.symbols] == ["WSPY", "WQQQ"]
        assert [x.triggered_closed for x in s.symbols] == [False, True]
        assert [round(x.value_closed, 2) for x in s.symbols] == [D("100.00"), D("0.00")]
        assert [x.value_now for x in s.symbols] == [None, None]  # no forming bar on 15m


async def test_selector_keeps_only_intraday_portfolios(env):
    await env.strategy("W-1", ["WSPY"], [env.p1, env.plain])
    await env.strategy("W-D", ["WSPY"], [env.p1], timeframe="1d")
    await env.strategy("W-H", ["WSPY"], [env.hourly])
    env.bars("WSPY", FALLING)
    live = {p.id: p for p in (env.p1, env.hourly)}  # the day-scope, non archived ones
    out = await strategy_signals(env.s, env.qs, now=NOW,
                                 select=intraday_selector(live, Timeframe.m15))
    assert [(s.ref, s.portfolio_id) for s in out] == [("W-1", env.p1.id)]


async def test_selector_rejects_timeframe_outside_intraday(env):
    await env.strategy("W-D", ["WSPY"], [env.p1], timeframe="1d")
    env.bars("WSPY", FALLING)
    out = await strategy_signals(env.s, env.qs, now=NOW,
                                 select=intraday_selector({env.p1.id: env.p1}, Timeframe.d1))
    assert out == []


async def test_selector_excludes_pairs(env):
    await env.strategy("W-1", ["WSPY", "WQQQ"], [env.p1])
    env.bars("WSPY", FALLING)
    env.bars("WQQQ", FALLING)
    sel = intraday_selector({env.p1.id: env.p1}, Timeframe.m15,
                            exclude=frozenset({(env.p1.id, "WSPY")}))
    [s] = await strategy_signals(env.s, env.qs, now=NOW, select=sel)
    assert [x.symbol for x in s.symbols] == ["WQQQ"]


async def test_entry_fires_once(env):
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", FALLING)
    key = STATE_KEY.format(portfolio_id=env.p1.id, ref="W-1", symbol="WSPY", rule="entry")
    assert await env.run() == []  # missing key: the value is stored, no trigger
    assert env.redis.data[key] == "1" and env.redis.ttl[key] == STATE_TTL_S
    env.prev(env.p1, "W-1", "WSPY", "entry", "0")
    [t] = await env.run()
    assert (t.portfolio_id, t.ref, t.name, t.symbol, t.rule, t.timeframe, t.bar_close) == (
        env.p1.id, "W-1", "Strategia W-1", "WSPY", "entry", "15m", BAR_CLOSE)
    assert t.value is not None and round(t.value, 2) == D("0.00") and t.threshold == D(10)
    assert t.price == D(FALLING[-1]) and t.late is False
    assert t.rule_text == "RSI 2 sotto 10"
    assert env.redis.data[key] == "1"
    assert await env.run() == []  # still true: no new trigger


async def test_value_goes_back_to_zero(env):
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", RISING)
    env.prev(env.p1, "W-1", "WSPY", "entry", "1")
    assert await env.run() == []
    key = STATE_KEY.format(portfolio_id=env.p1.id, ref="W-1", symbol="WSPY", rule="entry")
    assert env.redis.data[key] == "0"


async def test_no_entry_with_position_or_resting_entry(env):
    await env.strategy("W-1", ["WSPY", "WQQQ"], [env.p1])
    env.bars("WSPY", FALLING)
    env.bars("WQQQ", FALLING)
    env.s.add(Position(portfolio_id=env.p1.id, symbol="WSPY", qty=D(1), avg_price=D(150)))
    env.s.add(Order(portfolio_id=env.p1.id, symbol="WQQQ", side=OrderSide.buy,
                    type=OrderType.limit, qty=D(1), limit_price=D(90),
                    status=OrderStatus.pending))
    await env.s.flush()
    for sym in ("WSPY", "WQQQ"):
        env.prev(env.p1, "W-1", sym, "entry", "0")
    assert await env.run() == []


async def test_exit_fires_with_position(env):
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", RISING)
    env.s.add(Position(portfolio_id=env.p1.id, symbol="WSPY", qty=D(1), avg_price=D(100)))
    await env.s.flush()
    env.prev(env.p1, "W-1", "WSPY", "exit", "0")
    [t] = await env.run()
    assert t.rule == "exit" and t.rule_text == "RSI 2 sopra 70"
    assert t.threshold == D(70) and t.late is False
    assert await env.run() == []


async def test_late_entry_after_cutoff(env):
    close = datetime(2026, 12, 1, 20, 50, tzinfo=UTC)  # NYSE closes 21:00Z
    env.p1.day_close_minutes = 10
    await env.strategy("W-1", ["WSPY", "WQQQ"], [env.p1])
    env.qs.set_bars("WSPY", _bars(FALLING, close))
    env.qs.set_bars("WQQQ", _bars(RISING, close))
    env.s.add(Position(portfolio_id=env.p1.id, symbol="WQQQ", qty=D(1), avg_price=D(100)))
    await env.s.flush()
    env.prev(env.p1, "W-1", "WSPY", "entry", "0")
    env.prev(env.p1, "W-1", "WQQQ", "exit", "0")
    got = {t.rule: t for t in await env.run(bar_close=close, now=close + timedelta(minutes=1))}
    assert got["entry"].late is True and got["exit"].late is False


async def test_other_market_symbols_ignored(env):
    await env.strategy("W-1", ["WSPY", "WENEL.MI"], [env.p1])
    env.bars("WSPY", FALLING)
    env.bars("WENEL.MI", FALLING)
    for sym in ("WSPY", "WENEL.MI"):
        env.prev(env.p1, "W-1", sym, "entry", "0")
    assert [t.symbol for t in await env.run()] == ["WSPY"]
    assert env.redis.data[STATE_KEY.format(
        portfolio_id=env.p1.id, ref="W-1", symbol="WENEL.MI", rule="entry")] == "0"


async def test_excluded_pair_and_other_portfolios(env):
    await env.strategy("W-1", ["WSPY"], [env.p1, env.p2, env.plain])
    env.bars("WSPY", FALLING)
    for p in (env.p1, env.p2, env.plain):
        env.prev(p, "W-1", "WSPY", "entry", "0")
    out = await env.run(exclude=frozenset({(env.p1.id, "WSPY")}))
    assert [t.portfolio_id for t in out] == [env.p2.id]  # p1 excluded, plain is not intraday


DROP = [100 + i for i in range(39)] + [50]  # RSI(2) high, then about 1 on the last bar
JUMP = [200 - i for i in range(39)] + [250]  # RSI(2) low, then about 99 on the last bar


async def test_first_evaluation_fires_when_the_previous_bar_was_false(env):
    """27/11 Prova A: the watch started at the 14:45 close, on the very bar the entries
    went true (sma_cross 3/8 across the overnight gap on XLF/XLE/XLK, RSI 3 under 40 on
    AAPL); with no stored state they were swallowed. The previous closed bar is in the
    same bars: false there and true now is a trigger, even on the first evaluation."""
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", DROP)
    [t] = await env.run()
    assert (t.symbol, t.rule) == ("WSPY", "entry")
    key = STATE_KEY.format(portfolio_id=env.p1.id, ref="W-1", symbol="WSPY", rule="entry")
    assert env.redis.data[key] == "1"
    assert await env.run() == []  # the state is stored now: no second trigger


async def test_first_evaluation_exit_fires_when_the_previous_bar_was_false(env):
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", JUMP)
    env.s.add(Position(portfolio_id=env.p1.id, symbol="WSPY", qty=D(1), avg_price=D(100)))
    await env.s.flush()
    [t] = await env.run()
    assert t.rule == "exit"


async def test_first_evaluation_with_the_rule_already_true_does_not_fire(env):
    await env.strategy("W-1", ["WSPY"], [env.p1])
    env.bars("WSPY", RISING)
    env.s.add(Position(portfolio_id=env.p1.id, symbol="WSPY", qty=D(1), avg_price=D(100)))
    await env.s.flush()
    assert await env.run() == []  # the exit was true on the previous bar already
