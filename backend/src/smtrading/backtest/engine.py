"""Pure long-only backtest engine: no I/O, no ledger, no broker. Decimal money.

Cash is split equally between symbols. A signal computed at a bar's close executes at the
next bar's open (buy_and_hold enters at the first bar's open) with slippage against the
trader; the quantity is the largest whole number of lots affordable net of the fee. Open
positions are marked at each bar's close and valued at the last close at the end.

`trade_from` (research windows): the bars before it only warm the indicators up. Nothing
executes before it (buy_and_hold enters at the first bar from it), the equity curve, the
metrics and the exposure start there, and a signal of the last bar before it executes at
its open, as it would have live. Without it every bar trades (the default).
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from smtrading.backtest import strategies
from smtrading.fees.calculator import FeeRuleData, compute_trade_fee
from smtrading.marketdata.types import Bar

MAX_EQUITY_POINTS = 1000
_PCT = Decimal("0.0001")
_BPS = Decimal(10_000)
_PERIODS_PER_YEAR = 252


def _q(x: Decimal) -> Decimal:
    return x.quantize(_PCT)


def _fee(rule: FeeRuleData, notional: Decimal) -> Decimal:
    return compute_trade_fee(rule, notional) if notional > 0 else Decimal(0)


def _affordable_lots(cash: Decimal, price: Decimal, lot: Decimal, rule: FeeRuleData) -> int:
    unit = lot * price
    if unit <= 0 or cash <= 0:
        return 0
    lots = int(cash // unit)
    for _ in range(10):
        if lots <= 0:
            return 0
        affordable = int(max(cash - _fee(rule, lots * unit), Decimal(0)) // unit)
        if affordable >= lots:
            return lots
        lots = affordable
    return 0


def _simulate_symbol(
    symbol: str, bars: list[Bar], strategy: str, params: dict, cash0: Decimal,
    rule: FeeRuleData, slip: Decimal, lot: Decimal, first: int = 0,
) -> tuple[dict[datetime, Decimal], list[dict], int]:
    sigs = strategies.signals(bars, strategy, params)
    immediate = strategy in strategies.IMMEDIATE
    cash, qty = cash0, Decimal(0)
    entry: dict[str, Any] = {}
    equity: dict[datetime, Decimal] = {}
    trades: list[dict] = []
    invested_bars = 0
    for i, bar in enumerate(bars):
        if i < first:
            continue  # warm-up only
        if immediate:
            act = "buy" if i == first else "hold"
        else:
            act = sigs[i - 1] if i > 0 else "hold"
        if act == "buy" and qty == 0:
            price = bar.open * (1 + slip)
            lots = _affordable_lots(cash, price, lot, rule)
            if lots > 0:
                qty = lots * lot
                fee = _fee(rule, qty * price)
                cash -= qty * price + fee
                entry = {"ts": bar.ts, "price": price, "fee": fee}
        elif act == "sell" and qty > 0:
            price = bar.open * (1 - slip)
            fee = _fee(rule, qty * price)
            proceeds = qty * price - fee
            pnl = proceeds - (qty * entry["price"] + entry["fee"])
            cash += proceeds
            trades.append({
                "symbol": symbol, "entry_ts": entry["ts"], "entry_price": entry["price"],
                "exit_ts": bar.ts, "exit_price": price, "qty": qty, "pnl": pnl,
                "fees": entry["fee"] + fee,
            })
            qty = Decimal(0)
        equity[bar.ts] = cash + qty * bar.close
        if qty > 0:
            invested_bars += 1
    return equity, trades, invested_bars


def _downsample(points: list[dict], cap: int) -> list[dict]:
    if len(points) <= cap:
        return points
    last = len(points) - 1
    idx = sorted({round(k * last / (cap - 1)) for k in range(cap)})
    return [points[i] for i in idx]


def _max_drawdown_pct(values: list[Decimal]) -> Decimal:
    peak, worst = Decimal(0), Decimal(0)
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return _q(worst * 100)


def _sharpe(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    daily: dict[Any, Decimal] = {}
    for ts, e in curve:
        daily[ts.date()] = e
    vals = list(daily.values())
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 2:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 28
        mean = sum(rets, Decimal(0)) / len(rets)
        var = sum(((r - mean) ** 2 for r in rets), Decimal(0)) / (len(rets) - 1)
        if var == 0:
            return Decimal(0)
        return _q(mean / var.sqrt() * Decimal(_PERIODS_PER_YEAR).sqrt())


def _cagr_pct(initial: Decimal, final: Decimal, first: datetime, last: datetime) -> Decimal:
    days = Decimal(str((last - first).total_seconds())) / 86400
    if days < 1 or initial <= 0:
        return Decimal(0)
    if final <= 0:
        return Decimal(-100)
    with localcontext() as ctx:
        ctx.prec = 28
        growth = (final / initial) ** (Decimal(365) / days)
        return _q((growth - 1) * 100)


def run_backtest(
    bars_by_symbol: dict[str, list[Bar]], strategy: str, params: dict, *,
    initial_cash: Decimal, fee_rule: FeeRuleData | Mapping[str, FeeRuleData],
    slippage_bps: int,
    lot_by_symbol: dict[str, Decimal],
    trade_from: datetime | None = None,
) -> dict:
    params = strategies.validate_params(strategy, params)
    need = strategies.min_bars(strategy, params)
    slip = Decimal(slippage_bps) / _BPS
    symbols = sorted(bars_by_symbol)
    share = initial_cash / len(symbols) if symbols else initial_cash
    warnings: list[str] = []
    curves: dict[str, dict[datetime, Decimal]] = {}
    trades: list[dict] = []
    exposure: list[Decimal] = []
    for sym in symbols:
        rule = fee_rule[sym] if isinstance(fee_rule, Mapping) else fee_rule
        bars = sorted(bars_by_symbol[sym], key=lambda b: b.ts)
        first = 0 if trade_from is None else next(
            (i for i, b in enumerate(bars) if b.ts >= trade_from), len(bars))
        if len(bars) < need or first == len(bars):
            warnings.append(f"insufficient_data:{sym}")
            curves[sym] = {b.ts: share for b in bars[first:]}
            continue
        eq, tr, inv = _simulate_symbol(
            sym, bars, strategy, params, share, rule, slip,
            lot_by_symbol.get(sym, Decimal(1)), first,
        )
        curves[sym] = eq
        trades.extend(tr)
        exposure.append(Decimal(inv) / (len(bars) - first) * 100)
    trades.sort(key=lambda t: (t["entry_ts"], t["symbol"]))

    all_ts = sorted({ts for c in curves.values() for ts in c})
    last_seen: dict[str, Decimal] = {s: share for s in symbols}
    curve: list[tuple[datetime, Decimal]] = []
    for ts in all_ts:
        for s in symbols:
            if ts in curves[s]:
                last_seen[s] = curves[s][ts]
        curve.append((ts, sum(last_seen.values(), Decimal(0))))

    final = curve[-1][1] if curve else initial_cash
    if len(curve) > 1 and curve[-1][0] - curve[0][0] < timedelta(days=365):
        warnings.append("cagr_annualised_from_short_span")
    wins = sum(1 for t in trades if t["pnl"] > 0)
    metrics = {
        "total_return_pct": _q((final / initial_cash - 1) * 100) if initial_cash else Decimal(0),
        "cagr_pct": _cagr_pct(initial_cash, final, curve[0][0], curve[-1][0])
        if len(curve) > 1 else Decimal(0),
        "max_drawdown_pct": _max_drawdown_pct([e for _, e in curve]),
        "sharpe": _sharpe(curve),
        "win_rate_pct": _q(Decimal(wins) / len(trades) * 100) if trades else Decimal(0),
        "trades": len(trades),
        "exposure_pct": _q(sum(exposure, Decimal(0)) / len(exposure)) if exposure
        else Decimal(0),
    }
    points = [{"ts": ts, "equity": _q(e)} for ts, e in curve]
    return {
        "metrics": metrics,
        "equity": _downsample(points, MAX_EQUITY_POINTS),
        "trades": trades,
        "warnings": warnings,
    }


__all__ = ["MAX_EQUITY_POINTS", "run_backtest"]
