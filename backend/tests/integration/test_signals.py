"""Signal engine: how close each approved strategy is to an entry, on the closed bar and
«se chiudesse ora». Source of truth: the CEO's `strategy` decisions (latest per ref wins),
completed from the backtest of `backtest_id` and the Quant's `other` decision of the ref."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.activity.models import Decision
from smtrading.agents import service as agents
from smtrading.backtest.models import Backtest, BacktestStatus
from smtrading.engine.quotesource import FakeMarketData
from smtrading.instruments.market_hours import set_closures
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.types import Bar
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.signals.service import strategy_signals, strategy_states

D = Decimal
NOW = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)  # Tuesday: NYSE open, MTA closed
DAY = timedelta(days=1)
LAST_CLOSED = datetime(2026, 11, 16, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


def _bars(closes: list[int]) -> list[Bar]:
    n = len(closes)
    return [Bar(LAST_CLOSED - DAY * (n - 1 - i), D(c), D(c + 1), D(c - 1), D(c), D(1))
            for i, c in enumerate(closes)]


class Env:
    def __init__(self, session):
        self.s = session
        self.qs = FakeMarketData()

    async def setup(self):
        self.ceo, _ = await agents.create_agent(self.s, "CEO SMT-signals")
        self.quant, _ = await agents.create_agent(self.s, "Quant SMT-signals")
        self.pru = Portfolio(name="Prudente S")
        self.agg = Portfolio(name="Aggressivo S")
        self.old = Portfolio(name="Archivio S", status=PortfolioStatus.archived)
        self.s.add_all([self.pru, self.agg, self.old])
        for sym, ac, market, ccy in (("ZZSPY", AssetClass.us_equity, "NYSE", "USD"),
                                     ("ZZQQQ", AssetClass.us_equity, "NASDAQ", "USD"),
                                     ("ZZENEL.MI", AssetClass.eu_equity, "MTA", "EUR")):
            if await self.s.get(Instrument, sym) is None:
                self.s.add(Instrument(symbol=sym, name=sym, asset_class=ac, market=market,
                                      currency=ccy))
        await self.s.flush()

    async def decision(self, kind="strategy", portfolio=None, symbols=(), agent=None,
                       summary="s", outcome=None, **details) -> Decision:
        d = Decision(agent_id=(agent or self.ceo).id, kind=kind, summary=summary,
                     details=details, symbols=list(symbols), outcome=outcome,
                     portfolio_id=portfolio.id if portfolio else None)
        self.s.add(d)
        await self.s.flush()
        return d

    async def backtest(self, strategy, symbols, params, portfolio=None, timeframe="1d"):
        b = Backtest(strategy=strategy, symbols=symbols, timeframe=timeframe,
                     start=NOW - DAY * 900, end=NOW - DAY * 10, initial_cash=D(500),
                     params=params, portfolio_id=portfolio.id if portfolio else None,
                     status=BacktestStatus.completed)
        self.s.add(b)
        await self.s.flush()
        return b

    async def run(self):
        return await strategy_signals(self.s, self.qs, now=NOW)


@pytest.fixture
async def env(db_session):
    e = Env(db_session)
    await e.setup()
    return e


def _by_ref(out):
    return {s.ref: s for s in out}


async def test_rsi_closed_and_now(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))  # rising: RSI(2) = 100
    env.qs.set_quote("ZZSPY", None, None, D(100), NOW - timedelta(seconds=10))  # sharp drop
    await env.decision(portfolio=env.pru, symbols=["ZZSPY"], ref="STR-001",
                       status="approvata", strategy="rsi_reversion", timeframe="1d",
                       params={"period": 2, "buy_below": 10, "sell_above": 70},
                       summary="Approvata STR-001 rsi 2")
    [s] = await env.run()
    assert (s.ref, s.name, s.type, s.timeframe, s.status) == (
        "STR-001", "Approvata STR-001 rsi 2", "rsi_reversion", "1d", "ok")
    assert (s.portfolio_id, s.portfolio_name) == (env.pru.id, "Prudente S")
    assert s.params == {"period": 2, "buy_below": D(10), "sell_above": D(70)}
    [x] = s.symbols
    assert x.symbol == "ZZSPY" and x.last == D(100) and x.direction == "below"
    assert x.indicator == "RSI 2" and x.threshold == D(10)
    assert x.value_closed == D(100) and x.triggered_closed is False
    assert x.value_now is not None and x.value_now < D(10) and x.triggered_now is True
    assert x.distance == x.value_now - D(10)
    assert x.as_of == NOW - timedelta(seconds=10)


async def test_closed_market_has_no_value_now(env):
    env.qs.set_bars("ZZENEL.MI", _bars([10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 2, 1, 1]))
    env.qs.set_quote("ZZENEL.MI", None, None, D(5), NOW - timedelta(minutes=1))
    await env.decision(portfolio=env.pru, symbols=["ZZENEL.MI"], ref="STR-002",
                       status="approvata", strategy="rsi_reversion", timeframe="1d",
                       params={"period": 2, "buy_below": 30, "sell_above": 70})
    [s] = await env.run()
    [x] = s.symbols
    assert s.status == "ok" and x.value_now is None
    assert x.triggered_now is x.triggered_closed  # nothing newer than the closed bar
    assert x.distance == x.value_closed - D(30)
    assert x.as_of == LAST_CLOSED and x.last == D(5)


async def test_stale_quote_has_no_value_now(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(100), NOW - timedelta(minutes=5))  # > 120 s
    await env.decision(portfolio=env.pru, symbols=["ZZSPY"], ref="STR-001",
                       status="approvata", strategy="rsi_reversion", timeframe="1d",
                       params={"period": 2, "buy_below": 10, "sell_above": 70})
    [x] = (await env.run())[0].symbols
    assert x.value_now is None and x.triggered_now is False and x.as_of == LAST_CLOSED


async def test_sma_cross_and_breakout(env):
    env.qs.set_bars("ZZQQQ", _bars(list(range(130, 100, -1))))  # falling: fast < slow
    env.qs.set_quote("ZZQQQ", None, None, D(200), NOW)
    await env.decision(portfolio=env.agg, symbols=["ZZQQQ"], ref="STR-106",
                       status="approvata", strategy="sma_cross", timeframe="1d",
                       params={"fast": 2, "slow": 5})
    await env.decision(portfolio=env.agg, symbols=["ZZQQQ"], ref="STR-107",
                       status="approvata", strategy="breakout", timeframe="1d",
                       params={"lookback": 3, "exit_lookback": 2})
    out = _by_ref(await env.run())
    [x] = out["STR-106"].symbols
    assert x.direction == "cross_up" and x.indicator == "SMA 2 / SMA 5"
    assert x.value_closed == D("101.5") and x.triggered_closed is False
    assert x.value_now == D("150.5") and x.threshold == D(122) and x.triggered_now is True
    assert x.distance == D(122) - D("150.5")
    [y] = out["STR-107"].symbols
    assert y.direction == "above" and y.indicator == "Chiusura / massimo 3"
    assert y.value_closed == D(101) and y.triggered_closed is False
    assert y.value_now == D(200) and y.threshold == D(104) and y.triggered_now is True


async def test_latest_decision_wins_and_suspended_disappear(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(129), NOW)
    common = {"strategy": "rsi_reversion", "timeframe": "1d", "symbols": ["ZZSPY"],
              "portfolio": env.pru}
    for ref, statuses in (("STR-A", ["approvata", "sospesa"]),
                          ("STR-B", ["approvata", "revocata"]),
                          ("STR-C", ["respinta"]),
                          ("STR-D", ["respinta", "approvata"])):
        for st in statuses:
            await env.decision(ref=ref, status=st, params={"period": 2}, **common)
    await env.decision(ref="STR-E", status="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-E", status="approvata", params={"period": 3}, **common)
    # the outcome column counts when details.status is missing
    await env.decision(ref="STR-F", outcome="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-H", status="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-H", outcome="sospesa", params={"period": 2}, **common)
    # a Quant registration is never an approval
    await env.decision(kind="other", agent=env.quant, ref="STR-G", status="approvata",
                       params={"period": 2}, **common)
    out = _by_ref(await env.run())
    assert set(out) == {"STR-D", "STR-E", "STR-F"}
    assert out["STR-E"].params["period"] == 3
    assert out["STR-E"].symbols[0].indicator == "RSI 3"


async def test_only_status_decisions_replace_and_modificata_overrides(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(129), NOW)
    common = {"strategy": "rsi_reversion", "timeframe": "1d", "symbols": ["ZZSPY"],
              "portfolio": env.pru}
    # an informative note (or one without any status) does not remove the approval
    await env.decision(ref="STR-I", status="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-I", status="informativa", params={"period": 9}, **common)
    await env.decision(ref="STR-N", status="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-N", params={"period": 9}, **common)
    await env.decision(ref="STR-O", outcome="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-O", outcome="informativa", **common)
    # «modificata» is an approval: its params override, the rest is kept
    await env.decision(ref="STR-M", status="approvata", params={"period": 2}, **common)
    await env.decision(ref="STR-M", status="modificata", params={"period": 4})
    # «modificata» alone is an approval too
    await env.decision(ref="STR-M2", status="modificata", params={"period": 5}, **common)
    # a later rejection still removes it
    await env.decision(ref="STR-R", status="modificata", params={"period": 2}, **common)
    await env.decision(ref="STR-R", status="respinta", **common)
    out = _by_ref(await env.run())
    assert set(out) == {"STR-I", "STR-N", "STR-O", "STR-M", "STR-M2"}
    assert out["STR-I"].params["period"] == 2 and out["STR-N"].params["period"] == 2
    m = out["STR-M"]
    assert m.status == "ok" and m.params["period"] == 4 and m.portfolio_id == env.pru.id
    assert [x.symbol for x in m.symbols] == ["ZZSPY"]
    assert m.symbols[0].indicator == "RSI 4"
    assert out["STR-M2"].params["period"] == 5


async def test_strategy_states_latest_status_and_name(env):
    """The «Strategie» page: every ref with a status decision, its latest status and name
    (a modificata keeps the approval's name unless it has a subject)."""
    await env.decision(ref="STR-1", status="approvata", summary="Approvata STR-1")
    await env.decision(ref="STR-1", status="informativa", summary="nota")
    await env.decision(ref="STR-1", status="sospesa", summary="Sospesa STR-1")
    await env.decision(strategy_id="STR-2", outcome="respinta", summary="Respinta STR-2")
    await env.decision(ref="STR-3", status="approvata", summary="Approvata STR-3")
    await env.decision(ref="STR-3", status="modificata", summary="Modificata STR-3")
    d = await env.decision(ref="STR-4", status="approvata", summary="Approvata STR-4")
    d.subject = "Tendenza"
    await env.decision(kind="other", agent=env.quant, ref="STR-9", status="approvata")
    out = await strategy_states(env.s)
    assert {r: (x.status, x.name) for r, x in out.items()} == {
        "STR-1": ("sospesa", "Sospesa STR-1"), "STR-2": ("respinta", "Respinta STR-2"),
        "STR-3": ("modificata", "Approvata STR-3"), "STR-4": ("approvata", "Tendenza")}


async def test_quant_registration_matched_on_strategy_id(env):
    env.qs.set_bars("ZZQQQ", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZQQQ", None, None, D(129), NOW)
    await env.decision(kind="other", agent=env.quant, strategy_id="STR-105",
                       strategy="sma_cross", params={"fast": 2, "slow": 5}, timeframe="1d")
    await env.decision(ref="STR-105", status="approvata", portfolio=env.pru,
                       symbols=["ZZQQQ"])
    s = _by_ref(await env.run())["STR-105"]
    assert (s.status, s.type, s.timeframe) == ("ok", "sma_cross", "1d")


async def test_fields_from_backtest_and_quant(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_bars("ZZQQQ", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(129), NOW)
    env.qs.set_quote("ZZQQQ", None, None, D(129), NOW)
    bt = await env.backtest("rsi_reversion", ["ZZSPY", "ZZQQQ"],
                            {"period": 2, "buy_below": "10", "sell_above": "70"}, env.agg)
    # like STR-002/103 in production: no timeframe, no symbols, no portfolio_id
    await env.decision(ref="STR-103", status="approvata", strategy="rsi_reversion",
                       params={"period": 2, "buy_below": 10, "sell_above": 70},
                       backtest_id=str(bt.id), portfolios=["AGG"])
    # only the ref: the type, params and timeframe of the Quant's registration
    await env.decision(kind="other", agent=env.quant, ref="STR-104",
                       strategy="sma_cross", params={"fast": 2, "slow": 5}, timeframe="1d")
    await env.decision(ref="STR-104", status="approvata", portfolio=env.pru,
                       symbols=["ZZQQQ"])
    out = _by_ref(await env.run())
    s = out["STR-103"]
    assert s.status == "ok" and s.timeframe == "1d" and s.portfolio_id == env.agg.id
    assert [x.symbol for x in s.symbols] == ["ZZSPY", "ZZQQQ"]
    s = out["STR-104"]
    assert (s.status, s.type, s.timeframe) == ("ok", "sma_cross", "1d")
    assert s.params == {"fast": 2, "slow": 5} and s.symbols[0].indicator == "SMA 2 / SMA 5"


async def test_incomplete_unsupported_and_archived(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(129), NOW)
    await env.decision(ref="STR-X", status="approvata", portfolio=env.pru,
                       symbols=["ZZSPY"], timeframe="1d")  # no strategy anywhere
    await env.decision(ref="STR-Y", status="approvata", portfolio=env.pru,
                       symbols=["ZZSPY"], timeframe="1d", strategy="buy_and_hold",
                       params={})
    await env.decision(ref="STR-Z", status="approvata", strategy="rsi_reversion",
                       timeframe="1d", params={"period": 2}, symbols=["ZZSPY"])  # nowhere
    await env.decision(ref="STR-W", status="approvata", portfolio=env.pru,
                       symbols=["ZZSPY", "ZZNOPE"], timeframe="1d",
                       strategy="rsi_reversion", params={"period": 2})  # unknown symbol
    await env.decision(ref="STR-V", status="approvata", portfolio=env.old,
                       symbols=["ZZSPY"], timeframe="1d", strategy="rsi_reversion",
                       params={"period": 2})  # archived portfolio
    await env.decision(ref="STR-U", status="approvata", portfolio=env.pru,
                       symbols=["ZZSPY"], timeframe="1d", strategy="rsi_reversion",
                       params={"period": "x"})  # invalid parameters
    out = _by_ref(await env.run())
    assert set(out) == {"STR-X", "STR-Y", "STR-Z", "STR-W", "STR-U"}
    assert out["STR-X"].status == "dati incompleti" and out["STR-X"].symbols == []
    assert out["STR-Y"].status == "non supportata" and out["STR-Y"].symbols == []
    assert out["STR-Z"].status == "dati incompleti" and out["STR-Z"].portfolio_id is None
    assert out["STR-U"].status == "dati incompleti"
    w = out["STR-W"]
    assert w.status == "dati incompleti"
    assert [(x.symbol, x.value_closed is None) for x in w.symbols] == [
        ("ZZSPY", False), ("ZZNOPE", True)]


async def test_one_strategy_in_two_portfolios(env):
    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(129), NOW)
    await env.decision(ref="STR-M", status="approvata", portfolio=env.pru,
                       portfolios=[str(env.agg.id), "PRU"], symbols=["ZZSPY"],
                       timeframe="1d", strategy="rsi_reversion", params={"period": 2})
    out = await env.run()
    assert sorted(s.portfolio_name for s in out) == ["Aggressivo S", "Prudente S"]


async def test_position_and_open_entry_per_portfolio(env):
    from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
    from smtrading.positions.models import Position

    env.qs.set_bars("ZZSPY", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZSPY", None, None, D(100), NOW - timedelta(seconds=10))
    env.qs.set_bars("ZZQQQ", _bars(list(range(100, 130))))
    env.qs.set_quote("ZZQQQ", None, None, D(100), NOW - timedelta(seconds=10))
    await env.decision(portfolio=env.pru, symbols=["ZZSPY", "ZZQQQ"], ref="STR-001",
                       status="approvata", strategy="rsi_reversion", timeframe="1d",
                       params={"period": 2, "buy_below": 10, "sell_above": 70},
                       portfolios=[str(env.agg.id)])
    env.s.add(Position(portfolio_id=env.agg.id, symbol="ZZSPY", qty=D("0.51"),
                       avg_price=D(258)))
    env.s.add(Position(portfolio_id=env.pru.id, symbol="ZZQQQ", qty=D(0), avg_price=D(0)))
    pending = {"side": OrderSide.buy, "type": OrderType.limit, "qty": D(1),
               "limit_price": D(90), "status": OrderStatus.pending}
    env.s.add_all([
        Order(portfolio_id=env.pru.id, symbol="ZZQQQ", **pending),  # a resting entry
        # not entries: filled, reduce-only, OCO leg, sell side, other portfolio's symbol
        Order(portfolio_id=env.agg.id, symbol="ZZQQQ", **{**pending, "status":
                                                          OrderStatus.filled}),
        Order(portfolio_id=env.agg.id, symbol="ZZQQQ", reduce_only=True, **pending),
        Order(portfolio_id=env.agg.id, symbol="ZZQQQ", oco_group_id=uuid.uuid4(), **pending),
        Order(portfolio_id=env.agg.id, symbol="ZZQQQ", **{**pending, "side": OrderSide.sell}),
        Order(portfolio_id=env.pru.id, symbol="ZZSPY", role="stop_loss", **pending),
    ])
    await env.s.flush()
    out = {s.portfolio_id: s for s in await env.run()}
    agg = {r.symbol: r for r in out[env.agg.id].symbols}
    pru = {r.symbol: r for r in out[env.pru.id].symbols}
    assert agg["ZZSPY"].position == {"qty": D("0.51"), "avg_price": D(258)}
    assert agg["ZZSPY"].open_entry is False and agg["ZZQQQ"].open_entry is False
    assert agg["ZZQQQ"].position is None
    assert pru["ZZSPY"].position is None and pru["ZZSPY"].open_entry is False
    assert pru["ZZQQQ"].position is None and pru["ZZQQQ"].open_entry is True
    # the indicator values stay
    assert agg["ZZSPY"].value_now is not None and agg["ZZSPY"].triggered_now is True


async def test_sma_cross_with_fast_1_is_the_close(env):
    env.qs.set_bars("ZZENEL.MI", _bars(list(range(130, 100, -1))))
    await env.decision(portfolio=env.pru, symbols=["ZZENEL.MI"], ref="STR-C1",
                       status="approvata", strategy="sma_cross", timeframe="1d",
                       params={"fast": 1, "slow": 5})
    [x] = (await env.run())[0].symbols
    assert x.indicator == "Chiusura / SMA 5" and x.direction == "cross_up"


async def _held(env, ref, strategy, params, closes):
    """A strategy on the closed-market symbol (closed bar only), held in Prudente."""
    from smtrading.positions.models import Position

    env.qs.set_bars("ZZENEL.MI", _bars(closes))
    await env.decision(portfolio=env.pru, symbols=["ZZENEL.MI"], ref=ref,
                       status="approvata", strategy=strategy, timeframe="1d",
                       params=params, portfolios=[str(env.agg.id)])
    env.s.add(Position(portfolio_id=env.pru.id, symbol="ZZENEL.MI", qty=D(2),
                       avg_price=D(10)))
    await env.s.flush()
    out = {s.portfolio_id: s for s in await env.run()}
    [held] = out[env.pru.id].symbols
    [flat] = out[env.agg.id].symbols
    assert flat.position is None and flat.exit is None  # no position: no exit rule
    return held


async def test_exit_rsi_reversion_both_sides(env):
    params = {"period": 2, "buy_below": 30, "sell_above": "70.5"}
    up = await _held(env, "STR-X1", "rsi_reversion", params, list(range(100, 130)))
    assert up.exit == {"rule": "RSI 2 sopra 70,5", "value": D(100), "threshold": D("70.5"),
                       "distance": D("-29.5"), "triggered": True}


async def test_exit_rsi_reversion_not_triggered(env):
    params = {"period": 2, "buy_below": 30, "sell_above": 70}
    down = await _held(env, "STR-X2", "rsi_reversion", params, list(range(130, 100, -1)))
    assert down.exit == {"rule": "RSI 2 sopra 70", "value": D(0), "threshold": D(70),
                         "distance": D(70), "triggered": False}


async def test_exit_sma_cross_both_sides(env):
    down = await _held(env, "STR-X3", "sma_cross", {"fast": 2, "slow": 5},
                       list(range(130, 100, -1)))  # fast 101.5 < slow 103
    assert down.exit == {"rule": "SMA 2 sotto SMA 5", "value": D("101.5"),
                         "threshold": D(103), "distance": D("-1.5"), "triggered": True}


async def test_exit_sma_cross_not_triggered_fast_1(env):
    up = await _held(env, "STR-X4", "sma_cross", {"fast": 1, "slow": 5},
                     list(range(100, 130)))  # close 129 > slow 127
    assert up.exit == {"rule": "Chiusura sotto SMA 5", "value": D(129),
                       "threshold": D(127), "distance": D(2), "triggered": False}


async def test_exit_breakout_both_sides(env):
    params = {"lookback": 3, "exit_lookback": 2}
    down = await _held(env, "STR-X5", "breakout", params, [10, 10, 10, 10, 5])
    assert down.exit == {"rule": "Chiusura sotto minimo 2", "value": D(5),
                         "threshold": D(9), "distance": D(-4), "triggered": True}


async def test_exit_breakout_not_triggered(env):
    params = {"lookback": 3, "exit_lookback": 2}
    flat = await _held(env, "STR-X6", "breakout", params, [10, 10, 10, 10, 10])
    assert flat.exit == {"rule": "Chiusura sotto minimo 2", "value": D(10),
                         "threshold": D(9), "distance": D(1), "triggered": False}
