"""Portfolio backtest: several strategies («sleeves», each a strategy, its params and its
symbols) sharing one capital. Pure (no I/O), long-only, Decimal money.

Signals are the backtest engine's (`strategies.signals`, computed at a bar's close from
the bars up to it) and execute at the next bar's open with slippage against the trader,
fees per symbol, whole lots. Each (sleeve, symbol) holds at most one position; two
sleeves may hold the same symbol.

Bar by bar on the common timeline (every symbol's timestamps), in this order:
1. exits on a sell signal, at the open;
2. entries on a buy signal (sleeve order, then symbol), refused (and counted) while
   `max_positions` positions are open, while the daily-loss breaker is tripped, or when
   the cash buys no lot. Size: `risk_pct` % of the equity (cash + positions at their last
   close) risked at a stop `stop_pct` % under the entry price, so a notional of equity ×
   risk_pct / stop_pct, capped at `max_weight_pct` % of the equity and at the cash;
3. stops: a position whose bar trades at or below its stop exits at the stop (at the
   open when the bar opens below it), the entry bar included;
4. the close marks the positions; the breaker compares the equity with the one at the
   start of the day: a loss of at least `max_daily_loss_pct` % of it (or
   `max_daily_loss_eur`, the lower of the two when both are set) trips it, and it keeps
   new entries off for the rest of that day and on the next bar (with daily bars, entries
   execute at the next open: the next session).

Result: metrics (the engine's, plus stops, breaker trips and refused entries), the equity
curve with its drawdown, per-sleeve contribution (net P/L over the initial cash, trades,
win rate, fees) and the correlation of the sleeves' daily returns (each sleeve's P/L
change over the previous day's equity; null with fewer than 3 days or a flat sleeve).
"""

import itertools
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from smtrading.backtest import strategies
from smtrading.backtest.engine import (
    MAX_EQUITY_POINTS,
    _affordable_lots,
    _cagr_pct,
    _downsample,
    _fee,
    _max_drawdown_pct,
    _q,
    _sharpe,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData
from smtrading.marketdata.types import Bar

MAX_SLEEVES = 5
MAX_PAIRS = 20
MAX_TRADES = 1000
DEFAULT_SIZING = {"risk_pct": Decimal(1), "stop_pct": Decimal(5), "max_positions": 10,
                  "max_weight_pct": Decimal(20)}
_BOUNDS = {"risk_pct": (Decimal("0.01"), Decimal(5)),
           "stop_pct": (Decimal("0.5"), Decimal(50)),
           "max_weight_pct": (Decimal(1), Decimal(100))}
_BPS = Decimal(10_000)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def validate_sleeves(sleeves: Any) -> list[dict]:
    """[{strategy, params (normalised), symbols (as given, deduplicated)}]."""
    if not isinstance(sleeves, list) or not sleeves:
        raise _invalid("sleeves", "sleeves must be a non-empty list of "
                       "{strategy, params, symbols}")
    if len(sleeves) > MAX_SLEEVES:
        raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED, f"at most {MAX_SLEEVES} sleeves",
                       {"limit": MAX_SLEEVES})
    out = []
    for i, s in enumerate(sleeves):
        if not isinstance(s, dict) or not isinstance(s.get("strategy"), str):
            raise _invalid(f"sleeves[{i}]", "each sleeve needs a strategy")
        unknown = set(s) - {"strategy", "params", "symbols"}
        if unknown:
            raise _invalid(f"sleeves[{i}]", f"unknown sleeve fields: {sorted(unknown)}")
        syms = s.get("symbols")
        if not isinstance(syms, list) or not syms or not all(
                isinstance(x, str) and x.strip() for x in syms):
            raise _invalid(f"sleeves[{i}].symbols", "symbols must be a non-empty list")
        out.append({"strategy": s["strategy"],
                    "params": strategies.validate_params(s["strategy"], s.get("params")),
                    "symbols": list(dict.fromkeys(syms))})
    pairs = sum(len(s["symbols"]) for s in out)
    if pairs > MAX_PAIRS:
        raise SMTError(ErrorCode.RISK_LIMIT_EXCEEDED,
                       f"at most {MAX_PAIRS} strategy x symbol pairs (got {pairs})",
                       {"limit": MAX_PAIRS})
    return out


def validate_sizing(sizing: Any) -> dict[str, Any]:
    if sizing is None:
        sizing = {}
    if not isinstance(sizing, dict):
        raise _invalid("sizing", "sizing must be an object")
    unknown = set(sizing) - set(DEFAULT_SIZING)
    if unknown:
        raise _invalid("sizing", f"unknown sizing fields: {sorted(unknown)}")
    out = dict(DEFAULT_SIZING)
    for k, v in sizing.items():
        if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
            raise _invalid(f"sizing.{k}", f"{k} must be a number")
        try:
            d = Decimal(str(v))
        except ArithmeticError:
            raise _invalid(f"sizing.{k}", f"{k} must be a number") from None
        if k == "max_positions":
            if not d.is_finite() or d != d.to_integral_value() or not 1 <= d <= MAX_PAIRS:
                raise _invalid("sizing.max_positions",
                               f"max_positions must be an integer 1-{MAX_PAIRS}")
            out[k] = int(d)
        else:
            lo, hi = _BOUNDS[k]
            if not d.is_finite() or not lo <= d <= hi:
                raise _invalid(f"sizing.{k}", f"{k} must be between {lo} and {hi}")
            out[k] = d
    return out


def _corr(a: list[Decimal], b: list[Decimal]) -> Decimal | None:
    n = len(a)
    if n < 3:
        return None
    with localcontext() as ctx:
        ctx.prec = 28
        ma, mb = sum(a, Decimal(0)) / n, sum(b, Decimal(0)) / n
        cov = sum(((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)), Decimal(0))
        va = sum(((x - ma) ** 2 for x in a), Decimal(0))
        vb = sum(((y - mb) ** 2 for y in b), Decimal(0))
        if va == 0 or vb == 0:
            return None
        return _q(cov / (va * vb).sqrt())


def run_portfolio_backtest(
    bars_by_symbol: dict[str, list[Bar]], sleeves: list[dict], *, initial_cash: Decimal,
    fee_rule: FeeRuleData | Mapping[str, FeeRuleData], slippage_bps: int,
    lot_by_symbol: dict[str, Decimal], sizing: dict | None = None,
    max_daily_loss_pct: Decimal | None = None, max_daily_loss_eur: Decimal | None = None,
) -> dict:
    sleeves = validate_sleeves(sleeves)
    size = validate_sizing(sizing)
    slip = Decimal(slippage_bps) / _BPS
    risk, stop_pct = size["risk_pct"] / 100, size["stop_pct"] / 100
    weight = size["max_weight_pct"] / 100
    bars = {s: sorted(b, key=lambda x: x.ts) for s, b in bars_by_symbol.items()}
    at: dict[str, dict[datetime, int]] = {
        s: {b.ts: i for i, b in enumerate(bs)} for s, bs in bars.items()}
    warnings: list[str] = []
    pairs: list[tuple[int, str]] = []
    sigs: dict[tuple[int, str], list[str]] = {}
    for k, sl in enumerate(sleeves):
        need = strategies.min_bars(sl["strategy"], sl["params"])
        for sym in sl["symbols"]:
            series = bars.get(sym, [])
            if len(series) < need:
                warnings.append(f"insufficient_data:{k + 1}:{sym}")
                continue
            pairs.append((k, sym))
            sigs[(k, sym)] = strategies.signals(series, sl["strategy"], sl["params"])

    def rule(sym: str) -> FeeRuleData:
        return fee_rule[sym] if isinstance(fee_rule, Mapping) else fee_rule

    timeline = sorted({b.ts for bs in bars.values() for b in bs})
    cash = initial_cash
    pos: dict[tuple[int, str], dict[str, Any]] = {}
    last_close: dict[str, Decimal] = {}
    realized = [Decimal(0)] * len(sleeves)
    fees = [Decimal(0)] * len(sleeves)
    closed = [[0, 0] for _ in sleeves]  # trades, wins
    trades: list[dict] = []
    counts = {"stops": 0, "breaker_trips": 0, "refused_max_positions": 0,
              "refused_breaker": 0, "refused_cash": 0}
    breaker_events: list[dict] = []
    curve: list[tuple[datetime, Decimal]] = []
    exposure: list[Decimal] = []
    sleeve_daily: dict[date, list[Decimal]] = {}
    equity_daily: dict[date, Decimal] = {}
    day: date | None = None
    day_start = initial_cash
    blocked_day: date | None = None
    blocked_index = -1

    def marked() -> Decimal:
        return cash + sum((p["qty"] * last_close.get(s, p["price"])
                           for (_, s), p in pos.items()), Decimal(0))

    def close(pair: tuple[int, str], price: Decimal, ts: datetime, reason: str) -> None:
        nonlocal cash
        p = pos.pop(pair)
        k, sym = pair
        fee = _fee(rule(sym), p["qty"] * price)
        proceeds = p["qty"] * price - fee
        pnl = proceeds - (p["qty"] * p["price"] + p["fee"])
        cash += proceeds
        realized[k] += pnl
        fees[k] += fee
        closed[k][0] += 1
        closed[k][1] += pnl > 0
        trades.append({"sleeve": k + 1, "symbol": sym, "entry_ts": p["ts"],
                       "entry_price": p["price"], "exit_ts": ts, "exit_price": price,
                       "qty": p["qty"], "pnl": pnl, "fees": p["fee"] + fee,
                       "exit": reason})

    def action(pair: tuple[int, str], i: int) -> str:
        if sleeves[pair[0]]["strategy"] in strategies.IMMEDIATE:
            return "buy" if i == 0 else "hold"
        return sigs[pair][i - 1] if i > 0 else "hold"

    for t, ts in enumerate(timeline):
        if ts.date() != day:
            day, day_start = ts.date(), marked()
        live = [(pair, at[pair[1]][ts]) for pair in pairs if ts in at[pair[1]]]
        for pair, i in live:  # 1. exits on signal
            if pair in pos and action(pair, i) == "sell":
                close(pair, bars[pair[1]][i].open * (1 - slip), ts, "signal")
        for pair, i in live:  # 2. entries
            if pair in pos or action(pair, i) != "buy":
                continue
            if blocked_day == day or blocked_index == t:
                counts["refused_breaker"] += 1
                continue
            if len(pos) >= size["max_positions"]:
                counts["refused_max_positions"] += 1
                continue
            sym = pair[1]
            bar = bars[sym][i]
            equity = marked()
            price = bar.open * (1 + slip)
            budget = min(equity * risk / stop_pct, equity * weight, cash)
            lots = _affordable_lots(budget, price, lot_by_symbol.get(sym, Decimal(1)),
                                    rule(sym))
            if lots <= 0:
                counts["refused_cash"] += 1
                continue
            qty = lots * lot_by_symbol.get(sym, Decimal(1))
            fee = _fee(rule(sym), qty * price)
            cash -= qty * price + fee
            fees[pair[0]] += fee
            pos[pair] = {"qty": qty, "price": price, "fee": fee, "ts": ts,
                         "stop": price * (1 - stop_pct)}
        for pair, i in live:  # 3. stops
            p = pos.get(pair)
            bar = bars[pair[1]][i]
            if p is not None and bar.low <= p["stop"]:
                counts["stops"] += 1
                close(pair, min(bar.open, p["stop"]) * (1 - slip), ts, "stop")
        for sym in bars:  # 4. close: marks and breaker
            if ts in at[sym]:
                last_close[sym] = bars[sym][at[sym][ts]].close
        equity = marked()
        curve.append((ts, equity))
        invested = equity - cash
        exposure.append(invested / equity * 100 if equity > 0 else Decimal(0))
        limits = [x for x in (
            day_start * max_daily_loss_pct / 100 if max_daily_loss_pct else None,
            max_daily_loss_eur or None) if x is not None]
        if limits and blocked_day != day and day_start - equity >= min(limits):
            counts["breaker_trips"] += 1
            blocked_day, blocked_index = day, t + 1
            breaker_events.append({"ts": ts, "loss": _q(day_start - equity),
                                   "day_start_equity": _q(day_start)})
        sleeve_daily[day] = [
            realized[k] + sum((p["qty"] * last_close[s] - p["qty"] * p["price"] - p["fee"]
                               for (kk, s), p in pos.items() if kk == k), Decimal(0))
            for k in range(len(sleeves))]
        equity_daily[day] = equity

    final = curve[-1][1] if curve else initial_cash
    days = sorted(sleeve_daily)
    rets: list[list[Decimal]] = [[] for _ in sleeves]
    for a, b in itertools.pairwise(days):
        base = equity_daily[a]
        for k in range(len(sleeves)):
            rets[k].append((sleeve_daily[b][k] - sleeve_daily[a][k]) / base
                           if base > 0 else Decimal(0))
    labels = [f"{k + 1}:{s['strategy']}" for k, s in enumerate(sleeves)]
    matrix = [[Decimal(1) if i == j else _corr(rets[i], rets[j])
               for j in range(len(sleeves))] for i in range(len(sleeves))]
    end_pnl = sleeve_daily[days[-1]] if days else [Decimal(0)] * len(sleeves)
    contribution = [{
        "sleeve": k + 1, "strategy": s["strategy"], "params": s["params"],
        "symbols": s["symbols"], "pnl": _q(end_pnl[k]), "realized_pnl": _q(realized[k]),
        "contribution_pct": _q(end_pnl[k] / initial_cash * 100),
        "trades": closed[k][0],
        "win_rate_pct": _q(Decimal(closed[k][1]) / closed[k][0] * 100) if closed[k][0]
        else Decimal(0),
        "fees": _q(fees[k]),
    } for k, s in enumerate(sleeves)]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    if len(curve) > 1 and curve[-1][0] - curve[0][0] < timedelta(days=365):
        warnings.append("cagr_annualised_from_short_span")
    metrics = {
        "total_return_pct": _q((final / initial_cash - 1) * 100),
        "cagr_pct": _cagr_pct(initial_cash, final, curve[0][0], curve[-1][0])
        if len(curve) > 1 else Decimal(0),
        "max_drawdown_pct": _max_drawdown_pct([e for _, e in curve]),
        "sharpe": _sharpe(curve),
        "win_rate_pct": _q(Decimal(wins) / len(trades) * 100) if trades else Decimal(0),
        "trades": len(trades),
        "exposure_pct": _q(sum(exposure, Decimal(0)) / len(exposure)) if exposure
        else Decimal(0),
        **counts,
    }
    peak = Decimal(0)
    points = []
    for ts, e in curve:
        peak = max(peak, e)
        points.append({"ts": ts, "equity": _q(e),
                       "drawdown_pct": _q((peak - e) / peak * 100) if peak > 0
                       else Decimal(0)})
    trades.sort(key=lambda x: (x["entry_ts"], x["sleeve"], x["symbol"]))
    if len(trades) > MAX_TRADES:
        warnings.append(f"trades_truncated:{MAX_TRADES}")
    return {
        "metrics": metrics,
        "equity": _downsample(points, MAX_EQUITY_POINTS),
        "contribution": contribution,
        "correlation": {"labels": labels, "matrix": matrix},
        "breaker_events": breaker_events[:100],
        "trades": trades[:MAX_TRADES],
        "sizing": size,
        "max_daily_loss_pct": max_daily_loss_pct,
        "max_daily_loss_eur": max_daily_loss_eur,
        "warnings": warnings,
    }


__all__ = ["MAX_PAIRS", "MAX_SLEEVES", "run_portfolio_backtest", "validate_sizing",
           "validate_sleeves"]
