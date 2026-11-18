"""momentum, donchian (with the ATR stop), the rsi_reversion trend filter and the
max_atr_pct volatility filter: exact trades on synthetic bars, no look-ahead, params."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.backtest import strategies
from smtrading.backtest.engine import run_backtest
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def rule() -> FeeRuleData:
    from uuid import uuid4
    return FeeRuleData(
        id=uuid4(), scope=FeeScope.global_, scope_ref=None, side=FeeSide.both,
        currency="EUR", fixed=D(0), pct=D(0), min_fee=None, max_fee=None, tiers=None,
        fx_pct=D(0), priority=0, active=True, created_at=T0,
    )


def flat(closes) -> list[Bar]:
    """open = high = low = close."""
    return [Bar(T0 + DAY * i, D(str(c)), D(str(c)), D(str(c)), D(str(c)), D(1))
            for i, c in enumerate(closes)]


def ohlc(rows) -> list[Bar]:
    """rows of (close, high, low); open = close."""
    return [Bar(T0 + DAY * i, D(str(c)), D(str(h)), D(str(lo)), D(str(c)), D(1))
            for i, (c, h, lo) in enumerate(rows)]


def run(bars, strategy, params, cash=1000):
    return run_backtest({"X": bars}, strategy, params, initial_cash=D(cash),
                        fee_rule=rule(), slippage_bps=0, lot_by_symbol={"X": D(1)})


def trade(out):
    return [(t["entry_ts"], t["entry_price"], t["exit_ts"], t["exit_price"], t["qty"],
             t["pnl"]) for t in out["trades"]]


# --- momentum --------------------------------------------------------------------------

MOM = [10, 10, 10, 11, 12, 13, 12, 11, 10, 9, 9]


def test_momentum_roc_and_trades():
    roc = strategies.roc([D(c) for c in MOM], 2, 0)
    assert roc[:2] == [None, None] and roc[2] == 0 and roc[3] == D(10)
    p = {"lookback": 2, "skip": 0, "enter_above": 5, "exit_below": 0}
    sigs = strategies.signals(flat(MOM), "momentum", p)
    # ROC 10, 20, 18.2 > 5: buy; 0 at bar 6 is between the bands: hold; then < 0: sell
    assert sigs == ["hold", "hold", "hold", "buy", "buy", "buy", "hold", "sell", "sell",
                    "sell", "sell"]
    # bought at the open after the first buy (bar 4), sold at the open after bar 7
    assert trade(run(flat(MOM), "momentum", p)) == [
        (T0 + DAY * 4, D(12), T0 + DAY * 8, D(10), D(83), D(83) * -2)]


def test_momentum_skip_uses_older_closes():
    p = {"lookback": 2, "skip": 1, "enter_above": 5, "exit_below": 0}
    roc = strategies.roc([D(c) for c in MOM], 2, 1)
    assert roc[:3] == [None] * 3 and roc[3] == 0 and roc[4] == D(10)  # close[3]/close[1]
    assert trade(run(flat(MOM), "momentum", p)) == [
        (T0 + DAY * 5, D(13), T0 + DAY * 9, D(9), D(76), D(76) * -4)]


def test_momentum_defaults_and_negative_bands():
    assert strategies.validate_params("momentum", {}) == {
        "lookback": 63, "skip": 0, "enter_above": D(0), "exit_below": D(0)}
    assert strategies.validate_params("momentum", {"enter_above": -2, "exit_below": "-5"})[
        "exit_below"] == D(-5)
    assert strategies.min_bars("momentum", strategies.validate_params(
        "momentum", {"lookback": 10, "skip": 3})) == 14


# --- donchian --------------------------------------------------------------------------

def test_donchian_channel_trades():
    bars = flat([10, 10, 10, 12, 13, 9, 8])
    p = {"entry": 3, "exit": 2}
    assert strategies.signals(bars, "donchian", p) == [
        "hold", "hold", "hold", "buy", "buy", "sell", "sell"]
    assert trade(run(bars, "donchian", p)) == [
        (T0 + DAY * 4, D(13), T0 + DAY * 6, D(8), D(76), D(76) * -5)]
    assert strategies.validate_params("donchian", {}) == {"entry": 20, "exit": 10}


# high = close + 1, low = close - 1: ATR(2) 2, 2, 2, 2.5, 2.25, 2.125, 2.0625, 2.03125...
STOP_BARS = ohlc([(c, c + 1, c - 1) for c in [10, 10, 10, 10, 12, 13, 13, 12, 11, 10]])


def test_donchian_atr_stop_exits_before_the_channel():
    p = {"entry": 3, "exit": 5}
    # buy at bar 4 (12 > 11); the 5-bar low channel is never broken: still open at the end
    assert run(STOP_BARS, "donchian", p)["trades"] == []
    stop = {**p, "atr_stop": 1, "atr_period": 2}
    sigs = strategies.signals(STOP_BARS, "donchian", stop)
    # bar 7: 12 > 14 - 2.0625; bar 8: 11 < 14 - 2.03125 (highest high of 3 bars - 1 ATR)
    assert sigs[4] == "buy" and sigs[7] == "hold" and sigs[8] == "sell"
    assert trade(run(STOP_BARS, "donchian", stop)) == [
        (T0 + DAY * 5, D(13), T0 + DAY * 9, D(10), D(76), D(76) * -3)]


def test_donchian_atr_stop_line():
    line = strategies.atr_stop_line(STOP_BARS, 3, D(1), 2)
    assert line[:1] == [None]
    assert line[4] == D("10.5") and line[8] == D("11.96875")
    assert strategies.validate_params("donchian", {"atr_stop": "1.5"}) == {
        "entry": 20, "exit": 10, "atr_stop": D("1.5"), "atr_period": 14}


# --- rsi_reversion trend filter -----------------------------------------------------------

def test_rsi_trend_filter_blocks_entries_below_the_sma():
    down = flat([20, 19, 18, 17, 16, 15, 16, 17, 18, 19, 20, 21])
    p = {"period": 2, "buy_below": 30, "sell_above": 70}
    # RSI(2) 0 at bars 2-5: bought at the open of 3, sold after RSI 75 at bar 7
    assert trade(run(down, "rsi_reversion", p)) == [
        (T0 + DAY * 3, D(17), T0 + DAY * 8, D(18), D(58), D(58))]
    # closes below SMA 5 on every oversold bar (or SMA not available yet): no entry
    assert run(down, "rsi_reversion", {**p, "sma_filter": 5})["trades"] == []


def test_rsi_trend_filter_allows_pullbacks_in_an_uptrend():
    up = flat([10, 11, 12, 13, 14, 15, 16, 15, 17, 18, 19, 20])
    p = {"period": 2, "buy_below": 60, "sell_above": 70, "sma_filter": 5}
    # bar 7: RSI 50 < 60 and close 15 > SMA5 14.6: bought at 17, RSI 83 sells at 18
    assert trade(run(up, "rsi_reversion", p)) == [
        (T0 + DAY * 8, D(17), T0 + DAY * 9, D(18), D(58), D(58))]
    assert strategies.min_bars("rsi_reversion", strategies.validate_params(
        "rsi_reversion", {"period": 2, "sma_filter": 200})) == 200


def test_rsi_without_filter_keeps_its_params():
    assert strategies.validate_params("rsi_reversion", {}) == {
        "period": 14, "buy_below": D(30), "sell_above": D(70)}


# --- max_atr_pct volatility filter ---------------------------------------------------------

ATR_BARS = ohlc([
    (10, "10.1", "9.9"), (10, "10.1", "9.9"), (10, "10.1", "9.9"), (10, "10.1", "9.9"),
    (12, 14, 10),  # breakout on a wide bar: ATR(2) 2.1 = 17.5% of the close
    (11, "11.1", "10.9"), (11, "11.1", "10.9"), (11, "11.1", "10.9"), (11, "11.1", "10.9"),
    ("11.5", "11.6", "11.4"),  # calm breakout: ATR(2) 0.4875 = 4.2%
    ("11.5", "11.6", "11.4"), (10, "10.1", "9.9"), (10, "10.1", "9.9"),
])


def test_max_atr_pct_skips_entries_on_volatile_bars():
    p = {"lookback": 3, "exit_lookback": 2}
    assert [t[0] for t in trade(run(ATR_BARS, "breakout", p))] == [T0 + DAY * 5]
    calm = {**p, "max_atr_pct": 10, "atr_period": 2}
    assert strategies.atr_pct(ATR_BARS, 2)[4] == D("17.5")
    assert trade(run(ATR_BARS, "breakout", calm)) == [
        (T0 + DAY * 10, D("11.5"), T0 + DAY * 12, D(10), D(86), D(86) * D("-1.5"))]
    # the sells are never filtered
    assert strategies.signals(ATR_BARS, "breakout", calm)[11] == "sell"
    assert strategies.validate_params("breakout", {"max_atr_pct": 3}) == {
        "lookback": 20, "exit_lookback": 10, "max_atr_pct": D(3), "atr_period": 14}


@pytest.mark.parametrize("strategy", ["sma_cross", "rsi_reversion", "breakout", "momentum",
                                      "donchian"])
def test_max_atr_pct_is_accepted_by_every_signal_strategy(strategy):
    out = strategies.validate_params(strategy, {"max_atr_pct": 5, "atr_period": 3})
    assert out["max_atr_pct"] == D(5) and out["atr_period"] == 3


# --- no look-ahead ---------------------------------------------------------------------------

# a choppy rise then a choppy fall: every rule (and filter) fires somewhere
WAVE = ohlc([(c, c + D("0.7"), c - D("0.4")) for c in
             (D(100) + D(i % 9) * 2 - D(i % 4) * 3 + (i if i < 40 else -i) for i in range(60))])


@pytest.mark.parametrize("strategy,params", [
    ("momentum", {"lookback": 5, "skip": 2, "enter_above": 1, "exit_below": -1}),
    ("donchian", {"entry": 4, "exit": 3, "atr_stop": "1.5", "atr_period": 3}),
    ("rsi_reversion", {"period": 3, "buy_below": 40, "sell_above": 60, "sma_filter": 6}),
    ("breakout", {"lookback": 4, "exit_lookback": 2, "max_atr_pct": 5, "atr_period": 3}),
    ("sma_cross", {"fast": 2, "slow": 5, "max_atr_pct": 5}),
])
def test_signals_use_only_data_up_to_the_bar(strategy, params):
    full = strategies.signals(WAVE, strategy, params)
    assert {"buy", "sell"} <= set(full)  # the series exercises both rules
    for i in range(len(WAVE)):
        assert strategies.signals(WAVE[: i + 1], strategy, params)[-1] == full[i], i


# --- validation -------------------------------------------------------------------------------

@pytest.mark.parametrize("strategy,params", [
    ("momentum", {"enter_above": 0, "exit_below": 1}),  # exit band above the entry one
    ("momentum", {"skip": -1}), ("momentum", {"lookback": 0}),
    ("momentum", {"enter_above": -101}), ("momentum", {"enter_above": "x"}),
    ("donchian", {"entry": 0}), ("donchian", {"atr_stop": -1}),
    ("donchian", {"lookback": 20}),
    ("rsi_reversion", {"sma_filter": -1}), ("rsi_reversion", {"sma_filter": 1.5}),
    ("breakout", {"atr_stop": 2}),  # the ATR stop is donchian's
    ("sma_cross", {"sma_filter": 50}),  # the trend filter is rsi_reversion's
    ("breakout", {"max_atr_pct": 101}), ("breakout", {"atr_period": 0}),
    ("buy_and_hold", {"max_atr_pct": 3}),  # no entry signal to filter
])
def test_invalid_new_params(strategy, params):
    with pytest.raises(SMTError) as e:
        strategies.validate_params(strategy, params)
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_zero_switches_are_off():
    p = {"lookback": 3, "exit_lookback": 2}
    base = strategies.signals(ATR_BARS, "breakout", p)
    assert strategies.signals(ATR_BARS, "breakout", {**p, "max_atr_pct": 0}) == base
    assert strategies.signals(STOP_BARS, "donchian", {"entry": 3, "exit": 5, "atr_stop": 0}
                              ) == strategies.signals(STOP_BARS, "donchian",
                                                      {"entry": 3, "exit": 5})
    assert "momentum" in strategies.STRATEGIES and "donchian" in strategies.STRATEGIES
