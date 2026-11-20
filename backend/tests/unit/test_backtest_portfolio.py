"""Portfolio backtest (backtest.portfolio): shared capital, sizing at the stop, max
positions / weight, stops, the daily-loss breaker, contribution and correlation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.backtest.portfolio import (
    run_portfolio_backtest,
    validate_sizing,
    validate_sleeves,
)
from smtrading.backtest.runner import zero_fee_rule
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def bars(rows) -> list[Bar]:
    """rows: closes, or (open, high, low, close) tuples; one bar a day."""
    out = []
    for i, r in enumerate(rows):
        o, h, lo, c = (r, r, r, r) if not isinstance(r, tuple) else r
        out.append(Bar(ts=T0 + timedelta(days=i), open=D(str(o)), high=D(str(h)),
                       low=D(str(lo)), close=D(str(c)), volume=D(1)))
    return out


def run(data, sleeves, **kw):
    args = {"initial_cash": D(10000), "fee_rule": zero_fee_rule(), "slippage_bps": 0,
            "lot_by_symbol": {}, **kw}
    return run_portfolio_backtest(data, sleeves, **args)


BH = "buy_and_hold"


def test_size_is_the_risk_at_the_stop_capped_by_the_weight():
    data = {"A": bars([100] * 5)}
    # 1% of 10000 at a 5% stop = 2000 notional (20 shares); weight cap 20% = 2000
    out = run(data, [{"strategy": BH, "symbols": ["A"]}],
              sizing={"risk_pct": 1, "stop_pct": 5, "max_weight_pct": 50})
    assert out["equity"][-1]["equity"] == D(10000)
    assert out["metrics"]["exposure_pct"] == D("20.0000")
    capped = run(data, [{"strategy": BH, "symbols": ["A"]}],
                 sizing={"risk_pct": 1, "stop_pct": 5, "max_weight_pct": 10})
    assert capped["metrics"]["exposure_pct"] == D("10.0000")


def test_max_positions_refuses_further_entries():
    data = {s: bars([100] * 4) for s in "ABC"}
    out = run(data, [{"strategy": BH, "symbols": ["A", "B", "C"]}],
              sizing={"max_positions": 2})
    assert out["metrics"]["refused_max_positions"] == 1
    assert out["metrics"]["exposure_pct"] == D("40.0000")  # 2 x 20%


def test_stop_exits_at_the_stop_or_at_a_gap_open():
    data = {"A": bars([100, 100, (99, 99, 94, 96), 96]),  # trades through the 95 stop
            "B": bars([100, 100, (90, 91, 89, 90), 90])}  # opens under it: exits at 90
    out = run(data, [{"strategy": BH, "symbols": ["A", "B"]}],
              sizing={"risk_pct": 1, "stop_pct": 5})
    by = {t["symbol"]: t for t in out["trades"]}
    assert by["A"]["exit"] == by["B"]["exit"] == "stop"
    assert by["A"]["exit_price"] == D(95) and by["B"]["exit_price"] == D(90)
    assert out["metrics"]["stops"] == 2
    assert by["A"]["pnl"] == 20 * D(-5) and by["B"]["pnl"] == 20 * D(-10)


def test_daily_loss_breaker_blocks_the_next_entries():
    # A is bought on day 0 and falls 30% on day 1 (no stop: stop_pct 50); the breaker
    # (2% of the day's starting equity) trips at day 1's close; B's breakout entry for
    # day 2's open is refused
    a = bars([100, (100, 100, 70, 70), 70, 70, 70])
    b = bars([10, 10, 10, 10, 10])
    sleeves = [{"strategy": BH, "symbols": ["A"]},
               {"strategy": "breakout", "params": {"lookback": 1, "exit_lookback": 1},
                "symbols": ["B"]}]
    b_up = bars([10, 11, 12, 13, 14])
    out = run({"A": a, "B": b_up}, sleeves, sizing={"stop_pct": 50, "risk_pct": 5},
              max_daily_loss_pct=D(2))
    assert out["metrics"]["breaker_trips"] == 1
    assert out["breaker_events"][0]["ts"] == a[1].ts
    assert out["metrics"]["refused_breaker"] == 1  # day 2's entry
    entries = [t["entry_ts"] for t in out["trades"] if t["symbol"] == "B"]
    assert a[2].ts not in entries
    off = run({"A": a, "B": b_up}, sleeves, sizing={"stop_pct": 50, "risk_pct": 5})
    assert off["metrics"]["breaker_trips"] == 0 and off["metrics"]["refused_breaker"] == 0
    eur = run({"A": a, "B": b}, sleeves, sizing={"stop_pct": 50, "risk_pct": 5},
              max_daily_loss_eur=D(100))
    assert eur["metrics"]["breaker_trips"] == 1


def test_contribution_and_correlation():
    up = bars([100 + i for i in range(10)])
    data = {"A": up, "B": up}
    out = run(data, [{"strategy": BH, "symbols": ["A"]}, {"strategy": BH, "symbols": ["B"]}])
    c1, c2 = out["contribution"]
    assert c1["pnl"] == c2["pnl"] == D(20 * 9)  # 20 shares, +9
    total = out["equity"][-1]["equity"] - 10000
    assert c1["pnl"] + c2["pnl"] == total
    assert c1["contribution_pct"] == D("1.8000")
    assert out["correlation"]["labels"] == ["1:buy_and_hold", "2:buy_and_hold"]
    assert out["correlation"]["matrix"][0][1] == 1  # the same daily returns
    wavy = bars([100, 103, 101, 106, 102, 108, 104, 110])
    other = bars([100, 97, 99, 94, 98, 92, 96, 90])
    out = run({"A": wavy, "B": other},
              [{"strategy": BH, "symbols": ["A"]}, {"strategy": BH, "symbols": ["B"]}],
              sizing={"stop_pct": 50, "risk_pct": 5})
    m = out["correlation"]["matrix"]
    assert m[0][0] == 1 and m[0][1] == m[1][0] and m[0][1] < D("-0.9")


def test_drawdown_curve_and_determinism():
    data = {"A": bars([100, 110, 99, 120])}
    out = run(data, [{"strategy": BH, "symbols": ["A"]}], sizing={"max_weight_pct": 100,
                                                                   "risk_pct": 5})
    dd = [p["drawdown_pct"] for p in out["equity"]]
    assert dd[0] == 0 and dd[2] > 0 and dd[3] == 0
    assert out == run(data, [{"strategy": BH, "symbols": ["A"]}],
                      sizing={"max_weight_pct": 100, "risk_pct": 5})


def test_insufficient_data_is_a_warning():
    out = run({"A": bars([100] * 3)},
              [{"strategy": "sma_cross", "symbols": ["A"]}])
    assert out["warnings"][0] == "insufficient_data:1:A" and out["trades"] == []


@pytest.mark.parametrize("sleeves, code", [
    ([], ErrorCode.INVALID_ORDER),
    ([{"strategy": BH}], ErrorCode.INVALID_ORDER),
    ([{"strategy": "nope", "symbols": ["A"]}], ErrorCode.INVALID_ORDER),
    ([{"strategy": BH, "symbols": ["A"], "extra": 1}], ErrorCode.INVALID_ORDER),
    ([{"strategy": BH, "symbols": ["A"]}] * 6, ErrorCode.RISK_LIMIT_EXCEEDED),
    ([{"strategy": BH, "symbols": [f"S{i}" for i in range(11)]}] * 2,
     ErrorCode.RISK_LIMIT_EXCEEDED),
])
def test_sleeve_limits(sleeves, code):
    with pytest.raises(SMTError) as e:
        validate_sleeves(sleeves)
    assert e.value.code == code


@pytest.mark.parametrize("sizing", [
    {"risk_pct": 0}, {"risk_pct": 6}, {"stop_pct": 0.1}, {"max_positions": 0},
    {"max_positions": 21}, {"max_positions": 1.5}, {"max_weight_pct": 101}, {"x": 1},
    {"risk_pct": True}, "big",
])
def test_sizing_limits(sizing):
    with pytest.raises(SMTError) as e:
        validate_sizing(sizing)
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_sizing_defaults():
    assert validate_sizing(None) == {"risk_pct": D(1), "stop_pct": D(5),
                                     "max_positions": 10, "max_weight_pct": D(20)}


def test_flat_sleeve_has_no_correlation():
    out = run({"A": bars([100] * 6), "B": bars([100, 101, 99, 102, 98, 103])},
              [{"strategy": BH, "symbols": ["A"]}, {"strategy": BH, "symbols": ["B"]}])
    assert out["correlation"]["matrix"][0][1] is None
