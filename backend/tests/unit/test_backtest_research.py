"""Walk-forward and parameter sweep on the backtest engine (backtest.research), and the
engine's `trade_from` warm-up."""

import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.backtest import research
from smtrading.backtest.engine import run_backtest
from smtrading.backtest.runner import zero_fee_rule
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata.types import Bar

D = Decimal
T0 = datetime(2021, 1, 1, tzinfo=UTC)
CFG = {"initial_cash": D(10000), "fee_rule": zero_fee_rule(), "slippage_bps": 5,
       "lot_by_symbol": {}}


def walk(n: int, seed: int, drift: float = 0.0004) -> list[Bar]:
    rnd = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        o = p
        p *= 1 + rnd.gauss(drift, 0.015) + 0.01 * math.sin(i / 15)
        c = p
        out.append(Bar(ts=T0 + timedelta(days=i), open=D(f"{o:.4f}"),
                       high=D(f"{max(o, c) * 1.004:.4f}"), low=D(f"{min(o, c) * 0.996:.4f}"),
                       close=D(f"{c:.4f}"), volume=D(1)))
    return out


def linear(closes) -> list[Bar]:
    return [Bar(ts=T0 + timedelta(days=i), open=D(c), high=D(c), low=D(c), close=D(c),
                volume=D(1)) for i, c in enumerate(closes)]


BARS = {"A": walk(600, 1), "B": walk(600, 2)}
GRID = {"fast": [5, 10, 20], "slow": [30, 60]}


def wf(bars=None, **kw):
    args = {"windows": 4, "is_pct": 70, "mode": "rolling", "metric": "sharpe",
            "max_combinations": 50, **CFG, **kw}
    return research.run_walk_forward(bars or BARS, "sma_cross", {}, GRID, **args)


# --- engine: trade_from ---------------------------------------------------------------

def test_trade_from_buy_and_hold_enters_at_the_first_bar_from_it():
    bars = linear([100 + i for i in range(20)])
    out = run_backtest({"X": bars}, "buy_and_hold", {}, trade_from=bars[10].ts, **{
        **CFG, "slippage_bps": 0})
    assert out["equity"][0]["ts"] == bars[10].ts  # the curve starts at trade_from
    assert out["equity"][0]["equity"] == D("10000.0000")  # bought at 110, closed at 110
    assert out["equity"][-1]["equity"] == D(10000) + 90 * 9  # 90 shares, 110 -> 119
    sliced = run_backtest({"X": bars[10:]}, "buy_and_hold", {}, **{**CFG, "slippage_bps": 0})
    assert out["metrics"] == sliced["metrics"]


def test_trade_from_warm_up_bars_only_feed_the_indicators():
    bars = walk(300, 7)
    cut = bars[150].ts
    full = run_backtest({"X": bars}, "sma_cross", {"fast": 5, "slow": 30}, **CFG)
    late = run_backtest({"X": bars}, "sma_cross", {"fast": 5, "slow": 30},
                        trade_from=cut, **CFG)
    assert all(t["entry_ts"] >= cut for t in late["trades"])
    assert late["equity"][0]["ts"] == cut
    # trades entirely after the cut are the same as without it, once flat at the cut
    flat_at_cut = all(not (t["entry_ts"] < cut <= t["exit_ts"]) for t in full["trades"])
    if flat_at_cut:
        assert [t for t in full["trades"] if t["entry_ts"] >= cut][:1] == late["trades"][:1]


def test_trade_from_after_the_last_bar_is_insufficient_data():
    bars = linear([100] * 5)
    out = run_backtest({"X": bars}, "buy_and_hold", {}, trade_from=T0 + timedelta(days=99),
                       **CFG)
    assert out["warnings"] == ["insufficient_data:X"] and out["trades"] == []


# --- windows and grid ----------------------------------------------------------------

def test_rolling_windows_tile_the_out_of_sample_up_to_the_last_bar():
    cuts = research.windows_of(1000, 4, 70, "rolling")
    assert cuts[-1][3] == 999
    for (a, b, c, d), nxt in zip(cuts, [*cuts[1:], None], strict=True):
        assert b == c - 1 and a <= b < c <= d
        assert (c - a) == (cuts[0][2] - cuts[0][0])  # same in-sample length (rolling)
        if nxt:
            assert nxt[2] == d + 1  # contiguous, no overlap
    oos = cuts[0][3] - cuts[0][2] + 1
    assert abs((cuts[0][2] - cuts[0][0]) / (cuts[0][2] - cuts[0][0] + oos) - 0.7) < 0.02


def test_anchored_windows_start_at_the_first_bar():
    cuts = research.windows_of(1000, 3, 60, "anchored")
    assert all(a == 0 for a, *_ in cuts)
    assert cuts[-1][3] == 999


@pytest.mark.parametrize("args, field", [
    ((1, 70, "rolling"), "windows"), ((11, 70, "rolling"), "windows"),
    ((4, 95, "rolling"), "is_pct"), ((4, 70, "sideways"), "mode"),
    ((True, 70, "rolling"), "windows"),
])
def test_window_arguments_are_checked(args, field):
    with pytest.raises(SMTError) as e:
        research.check_windows(*args)
    assert e.value.code == ErrorCode.INVALID_ORDER and e.value.details["field"] == field


def test_too_few_bars_for_the_windows():
    with pytest.raises(SMTError) as e:
        research.windows_of(30, 10, 70, "rolling")
    assert e.value.details["field"] == "windows"


def test_grid_expands_skips_invalid_combinations_and_dedupes():
    combos, varying, skipped = research.expand_grid(
        "sma_cross", {}, {"fast": [10, "10", 50], "slow": [20, 50]}, limit=200)
    assert varying == {"fast": [10, 50], "slow": [20, 50]}
    assert [(c["fast"], c["slow"]) for c in combos] == [(10, 20), (10, 50)]
    assert skipped == 2  # 50/20 and 50/50: fast must be lower than slow


def test_grid_limits():
    with pytest.raises(SMTError) as e:
        research.expand_grid("sma_cross", {}, {"fast": list(range(1, 21)),
                                               "slow": list(range(30, 41))}, limit=200)
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED and e.value.details["limit"] == 200
    with pytest.raises(SMTError) as e:
        research.grid_size({"fast": list(range(1, 22))})
    assert e.value.code == ErrorCode.RISK_LIMIT_EXCEEDED
    for bad in ({"nope": [1]}, {"fast": []}, {"fast": 5}, []):
        with pytest.raises(SMTError) as e:
            research.expand_grid("sma_cross", {}, bad, limit=200)
        assert e.value.code == ErrorCode.INVALID_ORDER
    with pytest.raises(SMTError) as e:  # every combination refused
        research.expand_grid("sma_cross", {}, {"fast": [60], "slow": [50]}, limit=200)
    assert e.value.details["field"] == "grid"


def test_unknown_metric():
    with pytest.raises(SMTError) as e:
        wf(metric="profit")
    assert e.value.details["field"] == "metric"


# --- walk-forward --------------------------------------------------------------------

def test_walk_forward_is_deterministic():
    assert wf() == wf()


def test_walk_forward_picks_the_in_sample_best_and_reports_each_window():
    out = wf()
    assert len(out["windows"]) == 4 and out["combinations"] == 6
    tl = sorted({b.ts for bars in BARS.values() for b in bars})
    cuts = research.windows_of(len(tl), 4, 70, "rolling")
    for row, (a, b, c, d) in zip(out["windows"], cuts, strict=True):
        assert (row["is_from"], row["is_to"], row["oos_from"], row["oos_to"]) == (
            tl[a], tl[b], tl[c], tl[d])
        is_bars = {s: [x for x in bs if tl[a] <= x.ts <= tl[b]] for s, bs in BARS.items()}
        scores = []
        for f in GRID["fast"]:
            for s in GRID["slow"]:
                m = run_backtest(is_bars, "sma_cross", {"fast": f, "slow": s}, **CFG)
                scores.append((m["metrics"]["sharpe"], -len(scores), {"fast": f, "slow": s}))
        assert row["best_params"] == max(scores, key=lambda x: (x[0], x[1]))[2]
    assert set(out["oos_metrics"]) >= {"total_return_pct", "max_drawdown_pct", "sharpe",
                                       "trades", "positive_windows_pct", "efficiency"}
    assert out["verdict"] in research.VERDICTS and out["reasons"]
    assert 0 <= out["stability"]["stability_pct"] <= 100


def test_walk_forward_never_reads_bars_after_a_window():
    base = wf()
    tl = sorted({b.ts for bars in BARS.values() for b in bars})
    end_w2 = base["windows"][1]["oos_to"]
    changed = {s: [b if b.ts <= end_w2 else Bar(ts=b.ts, open=b.open * 3, high=b.high * 3,
                                                 low=b.low / 3, close=b.close / 2,
                                                 volume=b.volume) for b in bs]
               for s, bs in BARS.items()}
    other = wf(changed)
    assert other["windows"][:2] == base["windows"][:2]  # windows 1-2 never saw the change
    assert other["windows"][2:] != base["windows"][2:]
    assert tl[-1] == base["windows"][-1]["oos_to"]


def test_walk_forward_out_of_sample_trades_stay_in_their_windows():
    out = wf()
    assert out["oos_metrics"]["trades"] == sum(
        w["oos_metrics"]["trades"] for w in out["windows"])
    first_oos = out["windows"][0]["oos_from"]
    assert all(p["ts"] >= first_oos for p in out["oos_equity"])


def test_walk_forward_anchored_and_buy_and_hold():
    out = research.run_walk_forward(BARS, "buy_and_hold", {}, {}, windows=3, is_pct=60,
                                    mode="anchored", metric="total_return_pct",
                                    max_combinations=50, **CFG)
    assert out["windows"][0]["is_from"] == out["windows"][2]["is_from"]
    assert out["stability"]["stability_pct"] == 100
    assert out["verdict"] != research.INSUFFICIENT  # buy_and_hold needs no trades


@pytest.mark.parametrize("agg, stability, verdict", [
    ({"trades": 3, "total_return_pct": D(5), "positive_windows_pct": D(100),
      "efficiency": D(1)}, D(100), research.INSUFFICIENT),
    ({"trades": 30, "total_return_pct": D(-1), "positive_windows_pct": D(75),
      "efficiency": D(1)}, D(100), research.DISCARD),
    ({"trades": 30, "total_return_pct": D(4), "positive_windows_pct": D(25),
      "efficiency": D(1)}, D(100), research.DISCARD),
    ({"trades": 30, "total_return_pct": D(4), "positive_windows_pct": D(75),
      "efficiency": D("0.2")}, D(100), research.FRAGILE),
    ({"trades": 30, "total_return_pct": D(4), "positive_windows_pct": D(75),
      "efficiency": D(1)}, D(25), research.FRAGILE),
    ({"trades": 30, "total_return_pct": D(4), "positive_windows_pct": D(75),
      "efficiency": None}, D(75), research.SOLID),
])
def test_verdict(agg, stability, verdict):
    got, reasons = research._verdict("sma_cross", agg, stability)
    assert got == verdict and reasons


# --- parameter sweep -----------------------------------------------------------------

def sweep(grid=None, **kw):
    args = {"metric": "sharpe", "sort": "metric", "max_combinations": 200, **CFG, **kw}
    return research.run_param_sweep(BARS, "sma_cross", {}, grid or GRID, **args)


def test_sweep_sorted_by_metric_with_ranks_and_heatmap():
    out = sweep()
    vals = [r["metrics"]["sharpe"] for r in out["rows"]]
    assert vals == sorted(vals, reverse=True) and len(vals) == 6
    assert [r["rank"] for r in out["rows"]] == list(range(1, 7))
    assert out["best_by_metric"] == out["rows"][0]["params"]
    hm = out["heatmap"]
    assert (hm["x_param"], hm["y_param"]) == ("fast", "slow")
    assert len(hm["z"]) == 2 and len(hm["z"][0]) == 3
    for r in out["rows"]:
        x = hm["x_values"].index(r["params"]["fast"])
        y = hm["y_values"].index(r["params"]["slow"])
        assert hm["z"][y][x] == r["metrics"]["sharpe"]
    assert sweep() == out  # deterministic


def test_sweep_robustness_is_the_median_over_the_neighbours():
    out = sweep()
    by = {(r["params"]["fast"], r["params"]["slow"]): r for r in out["rows"]}
    # (5, 30): neighbours (10, 30), (5, 60), (10, 60)
    own = by[(5, 30)]
    near = [by[k]["metrics"]["sharpe"] for k in ((10, 30), (5, 60), (10, 60))]
    assert own["neighbours"] == 3
    assert own["neighbour_median"] == sorted(near)[1]
    vals = sorted([own["metrics"]["sharpe"], *near])
    assert own["robust"] == ((vals[1] + vals[2]) / 2).quantize(D("0.0001"))
    robust = sweep(sort="robust")
    rs = [r["robust"] for r in robust["rows"]]
    assert rs == sorted(rs, reverse=True)
    assert robust["best_by_robust"] == robust["rows"][0]["params"]
    assert out["isolated_peak"] == (out["best_by_metric"] != out["best_by_robust"])


def test_sweep_drawdown_ranks_lowest_first_and_one_axis_has_no_heatmap():
    out = sweep({"fast": [5, 10, 20]}, metric="max_drawdown_pct")
    dd = [r["metrics"]["max_drawdown_pct"] for r in out["rows"]]
    assert dd == sorted(dd) and out["heatmap"] is None
    assert out["fixed_params"] == {"slow": 50}
