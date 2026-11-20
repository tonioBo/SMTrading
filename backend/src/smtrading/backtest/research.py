"""Strategy research on historical bars, on top of the backtest engine (pure: no I/O).

Every run goes through `engine.run_backtest` (signal at a bar's close, execution at the
next open with slippage, fees per symbol, whole lots), so costs and the no-look-ahead rule
are the backtests' ones.

Parameter grid (`grid`: {param: [values]}, over the strategy's fixed `params`): every
combination of the values, normalised like `strategies.validate_params`; combinations it
refuses (e.g. fast >= slow) are skipped and counted. At most MAX_VALUES_PER_PARAM values
per parameter; the number of combinations before skipping is bounded by the caller's
limit (RISK_LIMIT_EXCEEDED).

Metrics to rank by (METRICS): sharpe (default), total_return_pct, cagr_pct,
win_rate_pct (higher is better) and max_drawdown_pct (lower is better). Ties keep the
grid order (deterministic).

- walk_forward: the bar timeline (every symbol's timestamps) is cut into `windows`
  in-sample / out-of-sample pairs; out-of-sample segments are contiguous, do not overlap
  and end at the last bar; in-sample is `is_pct` % of a window (rolling: it slides with
  the window; anchored: it always starts at the first bar). Each window's best grid
  combination on the in-sample bars only is then run on the out-of-sample bars, the
  in-sample ones serving only as indicator warm-up (`trade_from`): no bar after a window's
  end is ever read for it. Aggregated out-of-sample metrics come from the out-of-sample
  equity curves chained one after the other; stability = how often the best combination
  changes between consecutive windows; verdict in VERDICTS.
- param_sweep: every combination on the whole period, sorted by the metric; `robust` is
  the median of the metric over the combination and its grid neighbours (one step away
  in any varying parameter), so an isolated peak ranks below a good plateau with
  `sort: "robust"`. With exactly two varying parameters a heatmap of the metric is added.
"""

import itertools
import statistics
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from smtrading.backtest import strategies
from smtrading.backtest.engine import (
    MAX_EQUITY_POINTS,
    _downsample,
    _max_drawdown_pct,
    _q,
    _sharpe,
    run_backtest,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.marketdata.types import Bar

METRICS = ("sharpe", "total_return_pct", "cagr_pct", "max_drawdown_pct", "win_rate_pct")
LOWER_IS_BETTER = frozenset({"max_drawdown_pct"})
MAX_VALUES_PER_PARAM = 20
MIN_WINDOWS, MAX_WINDOWS = 2, 10
MIN_IS_PCT, MAX_IS_PCT = 50, 90
MIN_OOS_TRADES = 10
MIN_WINDOW_BARS = 5
MODES = ("rolling", "anchored")
SOLID, FRAGILE, DISCARD, INSUFFICIENT = "solida", "fragile", "da scartare", "dati insufficienti"
VERDICTS = (SOLID, FRAGILE, DISCARD, INSUFFICIENT)
_SUMMARY = ("total_return_pct", "cagr_pct", "max_drawdown_pct", "sharpe", "win_rate_pct",
            "trades", "exposure_pct")


def _invalid(field: str, message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field, **details})


def check_metric(metric: str) -> str:
    if metric not in METRICS:
        raise _invalid("metric", f"metric must be one of {list(METRICS)}")
    return metric


def score(metrics: Mapping[str, Any], metric: str) -> Decimal:
    """Higher is better: the metric, negated when lower is better."""
    v = Decimal(str(metrics.get(metric, 0)))
    return -v if metric in LOWER_IS_BETTER else v


def grid_size(grid: Any) -> int:
    """Combinations of `grid` before validation (1 for an empty grid)."""
    if not isinstance(grid, dict):
        raise _invalid("grid", "grid must be an object {param: [values]}")
    n = 1
    for name, values in grid.items():
        if not isinstance(values, list) or not values:
            raise _invalid("grid", f"grid.{name} must be a non-empty list of values")
        if len(values) > MAX_VALUES_PER_PARAM:
            raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED,
                           f"grid.{name}: at most {MAX_VALUES_PER_PARAM} values",
                           {"limit": MAX_VALUES_PER_PARAM})
        n *= len(values)
    return n


def expand_grid(strategy: str, params: dict | None, grid: dict, *, limit: int
                ) -> tuple[list[dict], dict[str, list], int]:
    """(valid normalised combinations in grid order, the varying values per parameter,
    how many combinations validate_params refused). RISK_LIMIT_EXCEEDED over `limit`."""
    base = dict(params or {})
    strategies.validate_params(strategy, base)
    n = grid_size(grid)
    if n > limit:
        raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED,
                       f"at most {limit} parameter combinations (grid has {n})",
                       {"limit": limit, "combinations": n})
    axes: dict[str, list] = {}
    for name in sorted(grid):
        values: list = []
        for v in grid[name]:
            nv = strategies.normalize_value(strategy, name, v)
            if nv not in values:
                values.append(nv)
        axes[name] = values
    combos: list[dict] = []
    skipped = 0
    for point in itertools.product(*axes.values()):
        try:
            combos.append(strategies.validate_params(
                strategy, {**base, **dict(zip(axes, point, strict=True))}))
        except SMTError:
            skipped += 1
    if not combos:
        raise _invalid("grid", "no valid parameter combination in the grid")
    varying = {k: v for k, v in axes.items() if len(v) > 1}
    return combos, varying, skipped


def _summary(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {k: metrics[k] for k in _SUMMARY if k in metrics}


def _timeline(bars_by_symbol: Mapping[str, list[Bar]]) -> list[datetime]:
    return sorted({b.ts for bars in bars_by_symbol.values() for b in bars})


def _slice(bars_by_symbol: Mapping[str, list[Bar]], lo: datetime, hi: datetime
           ) -> dict[str, list[Bar]]:
    return {s: [b for b in bars if lo <= b.ts <= hi] for s, bars in bars_by_symbol.items()}


def check_windows(windows: Any, is_pct: Any, mode: Any) -> None:
    if mode not in MODES:
        raise _invalid("mode", f"mode must be one of {list(MODES)}")
    if isinstance(windows, bool) or not isinstance(windows, int) or not (
            MIN_WINDOWS <= windows <= MAX_WINDOWS):
        raise _invalid("windows", f"windows must be an integer {MIN_WINDOWS}-{MAX_WINDOWS}")
    if isinstance(is_pct, bool) or not isinstance(is_pct, int) or not (
            MIN_IS_PCT <= is_pct <= MAX_IS_PCT):
        raise _invalid("is_pct", f"is_pct must be an integer {MIN_IS_PCT}-{MAX_IS_PCT}")


def windows_of(n_bars: int, windows: int, is_pct: int, mode: str
               ) -> list[tuple[int, int, int, int]]:
    """(is_start, is_end, oos_start, oos_end) bar indexes, ends included, per window.
    Out-of-sample segments have equal length (the first absorbs the remainder) and end at
    the last bar; in-sample is is_pct % of a rolling window."""
    check_windows(windows, is_pct, mode)
    # n = is_len + windows * oos_len with is_len = oos_len * is_pct / (100 - is_pct)
    oos_len = n_bars * (100 - is_pct) // (100 * windows - (windows - 1) * is_pct)
    is_len = oos_len * is_pct // (100 - is_pct)
    if oos_len < MIN_WINDOW_BARS or is_len < MIN_WINDOW_BARS:
        raise _invalid("windows", "not enough bars for that many windows: fewer windows or "
                       "a longer period", bars=n_bars)
    first_oos = n_bars - windows * oos_len
    out = []
    for k in range(windows):
        oos_start = first_oos + k * oos_len
        is_start = 0 if mode == "anchored" else max(0, oos_start - is_len)
        out.append((is_start, oos_start - 1, oos_start, oos_start + oos_len - 1))
    return out


def _run(bars, strategy, params, cfg, trade_from=None) -> dict:
    return run_backtest(bars, strategy, params, trade_from=trade_from, **cfg)


def _best(bars, strategy, combos, metric, cfg) -> tuple[dict, dict, list[str]]:
    best: tuple[Decimal, dict, dict] | None = None
    warnings: set[str] = set()
    for combo in combos:
        out = _run(bars, strategy, combo, cfg)
        warnings.update(out["warnings"])
        s = score(out["metrics"], metric)
        if best is None or s > best[0]:
            best = (s, combo, out["metrics"])
    assert best is not None
    return best[1], best[2], sorted(warnings)


def run_walk_forward(
    bars_by_symbol: dict[str, list[Bar]], strategy: str, params: dict | None, grid: dict,
    *, windows: int, is_pct: int, mode: str, metric: str, initial_cash: Decimal,
    fee_rule: FeeRuleData | Mapping[str, FeeRuleData], slippage_bps: int,
    lot_by_symbol: dict[str, Decimal], max_combinations: int,
) -> dict:
    check_metric(metric)
    combos, varying, skipped = expand_grid(strategy, params, grid, limit=max_combinations)
    bars_by_symbol = {s: sorted(b, key=lambda x: x.ts) for s, b in bars_by_symbol.items()}
    timeline = _timeline(bars_by_symbol)
    cuts = windows_of(len(timeline), windows, is_pct, mode)
    cfg = {"initial_cash": initial_cash, "fee_rule": fee_rule,
           "slippage_bps": slippage_bps, "lot_by_symbol": lot_by_symbol}
    rows: list[dict] = []
    warnings: set[str] = set()
    chained: list[tuple[datetime, Decimal]] = []
    oos_trades: list[dict] = []
    factor = Decimal(1)
    for k, (a, b, c, d) in enumerate(cuts):
        is_bars = _slice(bars_by_symbol, timeline[a], timeline[b])
        best, is_metrics, w = _best(is_bars, strategy, combos, metric, cfg)
        warnings.update(f"window_{k + 1}:{x}" for x in w if x.startswith("insufficient"))
        # out of sample: the window's bars only (in-sample ones warm the indicators up)
        oos = _run(_slice(bars_by_symbol, timeline[a], timeline[d]), strategy, best, cfg,
                   trade_from=timeline[c])
        warnings.update(f"window_{k + 1}:{x}" for x in oos["warnings"]
                        if x.startswith("insufficient"))
        for p in oos["equity"]:
            chained.append((p["ts"], p["equity"] * factor))
        if oos["equity"]:
            factor = chained[-1][1] / initial_cash
        oos_trades.extend(oos["trades"])
        rows.append({
            "window": k + 1,
            "is_from": timeline[a], "is_to": timeline[b],
            "oos_from": timeline[c], "oos_to": timeline[d],
            "best_params": {n: best[n] for n in varying} if varying else {},
            "is_metrics": _summary(is_metrics), "oos_metrics": _summary(oos["metrics"]),
        })
    keys = [tuple(sorted(r["best_params"].items())) for r in rows]
    changes = sum(1 for i in range(1, len(keys)) if keys[i] != keys[i - 1])
    stability = _q(Decimal(100) * (1 - Decimal(changes) / (len(keys) - 1)))
    common, common_n = Counter(keys).most_common(1)[0]
    oos_ret = [Decimal(str(r["oos_metrics"]["total_return_pct"])) for r in rows]
    is_ret = [Decimal(str(r["is_metrics"]["total_return_pct"])) for r in rows]
    final = chained[-1][1] if chained else initial_cash
    wins = sum(1 for t in oos_trades if t["pnl"] > 0)
    mean_is = sum(is_ret, Decimal(0)) / len(is_ret)
    mean_oos = sum(oos_ret, Decimal(0)) / len(oos_ret)
    aggregate = {
        "total_return_pct": _q((final / initial_cash - 1) * 100),
        "max_drawdown_pct": _max_drawdown_pct([e for _, e in chained]),
        "sharpe": _sharpe(chained),
        "trades": len(oos_trades),
        "win_rate_pct": _q(Decimal(wins) / len(oos_trades) * 100) if oos_trades
        else Decimal(0),
        "positive_windows_pct": _q(Decimal(sum(1 for r in oos_ret if r > 0)) * 100
                                   / len(oos_ret)),
        "mean_is_return_pct": _q(mean_is),
        "mean_oos_return_pct": _q(mean_oos),
        "efficiency": _q(mean_oos / mean_is) if mean_is > 0 else None,
    }
    verdict, reasons = _verdict(strategy, aggregate, stability)
    points = [{"ts": ts, "equity": _q(e)} for ts, e in chained]
    return {
        "windows": rows,
        "oos_metrics": aggregate,
        "stability": {"param_changes": changes, "stability_pct": stability,
                      "most_common_params": dict(common), "most_common_windows": common_n},
        "verdict": verdict, "reasons": reasons,
        "oos_equity": _downsample(points, MAX_EQUITY_POINTS),
        "combinations": len(combos), "skipped_combinations": skipped,
        "varying_params": list(varying),
        "warnings": sorted(warnings),
    }


def _verdict(strategy: str, agg: dict, stability: Decimal) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if strategy not in strategies.IMMEDIATE and agg["trades"] < MIN_OOS_TRADES:
        return INSUFFICIENT, [(f"solo {agg['trades']} operazioni fuori campione "
                              f"(minimo {MIN_OOS_TRADES})")]
    ret, pos = agg["total_return_pct"], agg["positive_windows_pct"]
    if ret <= 0:
        reasons.append(f"rendimento fuori campione {ret}% (non positivo)")
    if pos < 50:
        reasons.append(f"finestre fuori campione positive solo {pos}%")
    if reasons:
        return DISCARD, reasons
    eff = agg["efficiency"]
    if eff is not None and eff < Decimal("0.5"):
        reasons.append(f"efficienza {eff}: fuori campione meno della metà del rendimento "
                       "in campione")
    if stability < 50:
        reasons.append(f"parametri migliori instabili (stabilità {stability}%)")
    if pos < 60:
        reasons.append(f"finestre fuori campione positive {pos}% (sotto il 60%)")
    if reasons:
        return FRAGILE, reasons
    return SOLID, [(f"rendimento fuori campione {ret}%, finestre positive {pos}%, "
                   f"stabilità {stability}%")]


def run_param_sweep(
    bars_by_symbol: dict[str, list[Bar]], strategy: str, params: dict | None, grid: dict,
    *, metric: str, sort: str, initial_cash: Decimal,
    fee_rule: FeeRuleData | Mapping[str, FeeRuleData], slippage_bps: int,
    lot_by_symbol: dict[str, Decimal], max_combinations: int,
) -> dict:
    check_metric(metric)
    if sort not in ("metric", "robust"):
        raise _invalid("sort", "sort must be metric or robust")
    combos, varying, skipped = expand_grid(strategy, params, grid, limit=max_combinations)
    cfg = {"initial_cash": initial_cash, "fee_rule": fee_rule,
           "slippage_bps": slippage_bps, "lot_by_symbol": lot_by_symbol}
    warnings: set[str] = set()
    rows: list[dict] = []
    for i, combo in enumerate(combos):
        out = _run(bars_by_symbol, strategy, combo, cfg)
        warnings.update(out["warnings"])
        rows.append({"order": i, "params": combo, "metrics": _summary(out["metrics"]),
                     "pos": tuple(varying[n].index(combo[n]) for n in varying)})
    by_pos = {r["pos"]: r for r in rows}
    for r in rows:
        near = [by_pos[p] for p in _neighbours(r["pos"]) if p in by_pos]
        vals = [Decimal(str(x["metrics"][metric])) for x in [r, *near]]
        r["neighbours"] = len(near)
        r["neighbour_median"] = _q(statistics.median(vals[1:])) if near else None
        r["robust"] = _q(statistics.median(vals))
    sign = -1 if metric in LOWER_IS_BETTER else 1

    def key_metric(r):
        return (-sign * Decimal(str(r["metrics"][metric])), r["order"])

    def key_robust(r):
        return (-sign * r["robust"], r["order"])

    best_metric = min(rows, key=key_metric)
    best_robust = min(rows, key=key_robust)
    rows.sort(key=key_robust if sort == "robust" else key_metric)
    heatmap = None
    if len(varying) == 2:
        x, y = list(varying)
        z: list[list[Any]] = [[None] * len(varying[x]) for _ in varying[y]]
        for r in rows:
            z[r["pos"][1]][r["pos"][0]] = r["metrics"][metric]
        heatmap = {"x_param": x, "y_param": y, "x_values": varying[x],
                   "y_values": varying[y], "metric": metric, "z": z}
    table = [{"rank": n + 1, "params": {k: r["params"][k] for k in varying} or r["params"],
              "metrics": r["metrics"], "neighbours": r["neighbours"],
              "neighbour_median": r["neighbour_median"], "robust": r["robust"]}
             for n, r in enumerate(rows)]
    return {
        "metric": metric, "sort": sort,
        "rows": table,
        "best_by_metric": {k: best_metric["params"][k] for k in varying},
        "best_by_robust": {k: best_robust["params"][k] for k in varying},
        "isolated_peak": best_metric is not best_robust,
        "heatmap": heatmap,
        "combinations": len(combos), "skipped_combinations": skipped,
        "varying_params": list(varying), "fixed_params": {
            k: v for k, v in combos[0].items() if k not in varying},
        "warnings": sorted(warnings),
    }


def _neighbours(pos: tuple[int, ...]) -> list[tuple[int, ...]]:
    """Grid points one step away in any varying parameter (diagonals included)."""
    out = []
    for delta in itertools.product((-1, 0, 1), repeat=len(pos)):
        if any(delta):
            out.append(tuple(p + d for p, d in zip(pos, delta, strict=True)))
    return out


__all__ = ["METRICS", "VERDICTS", "check_windows", "expand_grid", "grid_size", "run_param_sweep",
           "run_walk_forward", "score", "windows_of"]
