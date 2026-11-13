"""Pure long-only signal generators for the backtest engine.

`signals(bars, params)` returns one of "buy" | "sell" | "hold" per bar, computed from the
information available at that bar's close; the engine executes it at the next open.
Unknown strategies, unknown parameters and invalid values raise INVALID_ORDER.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata import indicators
from smtrading.marketdata.types import Bar

Signal = Literal["buy", "sell", "hold"]

# A strategy that enters on the very first bar's open instead of the next one.
IMMEDIATE = frozenset({"buy_and_hold"})

_DEFAULTS: dict[str, dict[str, Any]] = {
    "sma_cross": {"fast": 20, "slow": 50},
    "rsi_reversion": {"period": 14, "buy_below": Decimal(30), "sell_above": Decimal(70)},
    "breakout": {"lookback": 20, "exit_lookback": 10},
    "buy_and_hold": {},
}
STRATEGIES = tuple(_DEFAULTS)


def _invalid(message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, details)


def _int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise _invalid(f"{name} must be a positive integer", field=name)
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise _invalid(f"{name} must be a positive integer", field=name) from None
    if not d.is_finite() or d != d.to_integral_value() or d < 1 or d > 10_000:
        raise _invalid(f"{name} must be an integer between 1 and 10000", field=name)
    return int(d)


def _dec(v: Any, name: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise _invalid(f"{name} must be a number", field=name)
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise _invalid(f"{name} must be a number", field=name) from None
    if not d.is_finite() or d < 0 or d > 100:
        raise _invalid(f"{name} must be between 0 and 100", field=name)
    return d


def validate_params(strategy: str, params: dict | None) -> dict[str, Any]:
    """Defaults merged with `params`, normalised (ints and Decimals); INVALID_ORDER if bad."""
    if strategy not in _DEFAULTS:
        raise _invalid(f"unknown strategy {strategy!r}", field="strategy",
                       allowed=list(STRATEGIES))
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise _invalid("params must be an object", field="params")
    defaults = _DEFAULTS[strategy]
    unknown = sorted(set(params) - set(defaults))
    if unknown:
        raise _invalid(f"unknown parameters for {strategy}: {unknown}", field="params",
                       allowed=list(defaults))
    out = {**defaults, **params}
    for k, default in defaults.items():
        out[k] = _dec(out[k], k) if isinstance(default, Decimal) else _int(out[k], k)
    if strategy == "sma_cross" and out["fast"] >= out["slow"]:
        raise _invalid("fast must be lower than slow", field="params")
    if strategy == "rsi_reversion" and out["buy_below"] >= out["sell_above"]:
        raise _invalid("buy_below must be lower than sell_above", field="params")
    return out


def min_bars(strategy: str, params: dict[str, Any]) -> int:
    """Fewest bars for the strategy to be able to emit its first possible signal."""
    if strategy == "sma_cross":
        return params["slow"] + 1
    if strategy == "rsi_reversion":
        return params["period"] + 1
    if strategy == "breakout":
        return max(params["lookback"], params["exit_lookback"]) + 1
    return 1


def sma_cross(bars: list[Bar], params: dict) -> list[Signal]:
    closes = [b.close for b in bars]
    fast = indicators.sma(closes, params["fast"])
    slow = indicators.sma(closes, params["slow"])
    out: list[Signal] = ["hold"] * len(bars)
    for i in range(1, len(bars)):
        f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            continue
        if f0 <= s0 and f1 > s1:
            out[i] = "buy"
        elif f0 >= s0 and f1 < s1:
            out[i] = "sell"
    return out


def rsi_reversion(bars: list[Bar], params: dict) -> list[Signal]:
    values = indicators.rsi([b.close for b in bars], params["period"])
    out: list[Signal] = []
    for v in values:
        if v is None:
            out.append("hold")
        elif v < params["buy_below"]:
            out.append("buy")
        elif v > params["sell_above"]:
            out.append("sell")
        else:
            out.append("hold")
    return out


def breakout(bars: list[Bar], params: dict) -> list[Signal]:
    n, entry, exit_ = len(bars), params["lookback"], params["exit_lookback"]
    out: list[Signal] = ["hold"] * n
    for i in range(n):
        if i >= entry and bars[i].close > max(b.high for b in bars[i - entry : i]):
            out[i] = "buy"
        elif i >= exit_ and bars[i].close < min(b.low for b in bars[i - exit_ : i]):
            out[i] = "sell"
    return out


def buy_and_hold(bars: list[Bar], params: dict) -> list[Signal]:
    return ["buy" if i == 0 else "hold" for i in range(len(bars))]


_IMPL: dict[str, Callable[[list[Bar], dict], list[Signal]]] = {
    "sma_cross": sma_cross,
    "rsi_reversion": rsi_reversion,
    "breakout": breakout,
    "buy_and_hold": buy_and_hold,
}


def signals(bars: list[Bar], strategy: str, params: dict | None = None) -> list[Signal]:
    return _IMPL[strategy](bars, validate_params(strategy, params))
