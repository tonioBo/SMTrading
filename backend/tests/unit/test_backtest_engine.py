from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from smtrading.backtest import strategies
from smtrading.backtest.engine import MAX_EQUITY_POINTS, _max_drawdown_pct, run_backtest
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def rule(**kw) -> FeeRuleData:
    base = {
        "id": uuid4(), "scope": FeeScope.global_, "scope_ref": None, "side": FeeSide.both,
        "currency": "EUR", "fixed": D(0), "pct": D(0), "min_fee": None, "max_fee": None,
        "tiers": None, "fx_pct": D(0), "priority": 0, "active": True, "created_at": T0,
    }
    base.update({k: D(str(v)) if k in ("fixed", "pct") else v for k, v in kw.items()})
    return FeeRuleData(**base)


def series(closes, opens=None) -> list[Bar]:
    opens = opens or closes
    return [
        Bar(ts=T0 + timedelta(days=i), open=D(str(o)), high=D(str(max(o, c))),
            low=D(str(min(o, c))), close=D(str(c)), volume=D(1))
        for i, (o, c) in enumerate(zip(closes, opens, strict=True))
    ]


def run(bars, strategy="buy_and_hold", params=None, *, cash=1000, fee=None, slip=0, lot=1):
    return run_backtest(
        {"X": bars}, strategy, params or {}, initial_cash=D(cash), fee_rule=fee or rule(),
        slippage_bps=slip, lot_by_symbol={"X": D(lot)},
    )


def test_buy_and_hold_linear_rise():
    bars = series([100 + i for i in range(11)])  # 100 -> 110
    out = run(bars, cash=10000)
    m = out["metrics"]
    assert m["total_return_pct"] == D("10.0000")
    assert m["trades"] == 0 and out["warnings"] == ["cagr_annualised_from_short_span"]
    assert out["equity"][0]["equity"] == D("10000.0000")
    assert out["equity"][-1]["equity"] == D("11000.0000")


def test_buy_and_hold_loses_the_lot_rounding_remainder():
    out = run(series([300, 330]), cash=1000)  # 3 shares = 900, 100 cash left
    assert out["equity"][-1]["equity"] == D("1090.0000")
    assert out["metrics"]["total_return_pct"] == D("9.0000")


def test_sma_cross_single_trade_at_expected_dates():
    closes = [10, 10, 10, 10, 11, 12, 13, 12, 10, 8, 7, 6]
    out = run(series(closes), "sma_cross", {"fast": 2, "slow": 4}, cash=1000)
    [t] = out["trades"]
    # fast crosses above slow at index 4 (executes at open of 5), below at 8 (open of 9)
    assert t["entry_ts"] == T0 + timedelta(days=5) and t["entry_price"] == D(12)
    assert t["exit_ts"] == T0 + timedelta(days=9) and t["exit_price"] == D(8)
    assert t["qty"] == D(83) and t["pnl"] == D(83) * (D(8) - D(12))
    assert out["metrics"]["trades"] == 1 and out["metrics"]["win_rate_pct"] == D(0)


def test_fees_and_slippage_are_applied():
    closes = [10, 10, 10, 10, 11, 12, 13, 12, 10, 8, 7, 6]
    bars = series(closes)
    free = run(bars, "sma_cross", {"fast": 2, "slow": 4})
    paid = run(bars, "sma_cross", {"fast": 2, "slow": 4}, fee=rule(fixed=1, pct="0.5"),
               slip=100)
    [t] = paid["trades"]
    assert t["entry_price"] == D(12) * D("1.01") and t["exit_price"] == D(8) * D("0.99")
    assert t["fees"] > 2
    assert paid["metrics"]["total_return_pct"] < free["metrics"]["total_return_pct"]
    assert t["qty"] * t["entry_price"] <= D(1000)


def test_never_buys_more_than_cash_net_of_fee():
    out = run(series([100, 100]), cash=1000, fee=rule(pct=2))  # 9 shares: 900 + 18 fee
    assert out["equity"][-1]["equity"] == D("982.0000")


def test_insufficient_data_warns_and_does_not_trade():
    out = run(series([10] * 5), "sma_cross", {"fast": 2, "slow": 10})
    assert out["warnings"] == ["insufficient_data:X", "cagr_annualised_from_short_span"]
    assert out["trades"] == [] and out["metrics"]["total_return_pct"] == D(0)


def test_cash_is_split_equally_between_symbols():
    bars = series([100, 110])
    out = run_backtest(
        {"A": bars, "B": series([100, 90])}, "buy_and_hold", {}, initial_cash=D(1000),
        fee_rule=rule(), slippage_bps=0, lot_by_symbol={},
    )
    assert out["equity"][-1]["equity"] == D("1000.0000")  # +50 and -50


def test_max_drawdown_on_known_series():
    assert _max_drawdown_pct([D(100), D(120), D(90), D(110), D(60), D(130)]) == D("50.0000")
    out = run(series([100, 120, 90, 110, 60, 130]), cash=1000)
    assert out["metrics"]["max_drawdown_pct"] == D("50.0000")


def test_equity_curve_is_capped():
    out = run(series([100 + (i % 7) for i in range(2500)]))
    assert len(out["equity"]) == MAX_EQUITY_POINTS
    assert out["equity"][-1]["ts"] == T0 + timedelta(days=2499)


def test_sharpe_and_cagr_on_steady_growth():
    out = run(series([100 * 1.01**i for i in range(400)]), cash=100000)
    m = out["metrics"]
    assert m["cagr_pct"] > 0 and m["sharpe"] >= 0


def test_breakout_and_rsi_signals():
    bars = series([10, 10, 10, 12, 13, 9, 8])
    s = strategies.signals(bars, "breakout", {"lookback": 3, "exit_lookback": 2})
    assert s[3] == "buy" and s[5] == "sell"
    r = strategies.signals(series([10, 9, 8, 7, 6, 7, 9, 12, 15]), "rsi_reversion",
                           {"period": 3, "buy_below": 30, "sell_above": 70})
    assert "buy" in r and r[-1] == "sell"


@pytest.mark.parametrize("strategy,params", [
    ("nope", {}), ("sma_cross", {"fast": 50, "slow": 20}), ("sma_cross", {"bogus": 1}),
    ("sma_cross", {"fast": 0}), ("rsi_reversion", {"buy_below": 80}),
    ("breakout", {"lookback": "x"}), ("buy_and_hold", {"a": 1}),
])
def test_invalid_params(strategy, params):
    with pytest.raises(SMTError) as e:
        strategies.validate_params(strategy, params)
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_short_span_warns_and_per_symbol_rules():
    out = run(series([100, 101, 102]))
    assert "cagr_annualised_from_short_span" in out["warnings"]
    bars = series([100, 100, 100])
    res = run_backtest(
        {"A": bars, "B": bars}, "buy_and_hold", {}, initial_cash=D(1000),
        fee_rule={"A": rule(), "B": rule(fixed=10)}, slippage_bps=0, lot_by_symbol={},
    )
    assert res["equity"][-1]["equity"] == D("990.0000")  # only B pays its 10 fee
