"""Pure long-only signal generators for the backtest engine.

`signals(bars, params)` returns one of "buy" | "sell" | "hold" per bar, computed only from
the bars up to and including that bar (its close); the engine executes it at the next
open. Unknown strategies, unknown parameters and invalid values raise INVALID_ORDER.

Strategies (defaults in `_DEFAULTS`):
- sma_cross: buy when the `fast` SMA crosses above the `slow` one, sell on the opposite
  cross.
- rsi_reversion: buy with RSI(`period`) below `buy_below`, sell above `sell_above`.
  Optional trend filter `sma_filter` (n > 0): buy only while the close is above SMA(n).
- breakout: buy on a close above the highest high of the previous `lookback` bars, sell on
  a close below the lowest low of the previous `exit_lookback` bars.
- momentum: ROC = close[i - skip] / close[i - skip - lookback] - 1, in percent; buy while
  ROC > `enter_above`, sell when ROC < `exit_below` (`exit_below` <= `enter_above`; in
  between: hold).
- donchian: buy on a close above the highest high of the previous `entry` bars, sell on a
  close below the lowest low of the previous `exit` bars. Optional ATR stop `atr_stop`
  (multiplier k > 0): also sell when the close falls below the highest high of the last
  `entry` bars (this one included) minus k × ATR(`atr_period`) — a trailing stop that
  follows the channel's top, evaluated at the close like the other rules.
- buy_and_hold: buy at the first bar's open and hold (the benchmark).

Volatility filter for every strategy but buy_and_hold: `max_atr_pct` (> 0) turns a buy
into hold when ATR(`atr_period`) / close × 100 on that bar is above it (or not computable
yet); sells are never filtered. `atr_period` defaults to 14 and is added to the
normalised params whenever `max_atr_pct` or `atr_stop` is given.

Optional parameters (sma_filter, atr_stop, max_atr_pct, atr_period) appear in the
normalised params only when given; 0 switches them off.
"""

from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Literal

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata import indicators
from smtrading.marketdata.types import Bar

Signal = Literal["buy", "sell", "hold"]

# A strategy that enters on the very first bar's open instead of the next one.
IMMEDIATE = frozenset({"buy_and_hold"})
DEFAULT_ATR_PERIOD = 14

_DEFAULTS: dict[str, dict[str, Any]] = {
    "sma_cross": {"fast": 20, "slow": 50},
    "rsi_reversion": {"period": 14, "buy_below": Decimal(30), "sell_above": Decimal(70)},
    "breakout": {"lookback": 20, "exit_lookback": 10},
    "momentum": {"lookback": 63, "skip": 0, "enter_above": Decimal(0),
                 "exit_below": Decimal(0)},
    "donchian": {"entry": 20, "exit": 10},
    "buy_and_hold": {},
}
STRATEGIES = tuple(_DEFAULTS)
# the volatility filter: every strategy with an entry signal
_FILTERED = frozenset(STRATEGIES) - IMMEDIATE
_VOLATILITY = {"max_atr_pct": Decimal(0), "atr_period": DEFAULT_ATR_PERIOD}
_OPTIONAL: dict[str, dict[str, Any]] = {
    "rsi_reversion": {"sma_filter": 0},
    "donchian": {"atr_stop": Decimal(0)},
}
# value kinds: int 1-10000 (default for ints), int0 0-10000, pct 0-100 (default for
# Decimals), signed -100..1000 (a ROC in percent)
_KINDS = {"skip": "int0", "sma_filter": "int0",
          "enter_above": "signed", "exit_below": "signed"}
_Q = Decimal("1E-8")


def _invalid(message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, details)


def _number(v: Any, name: str, what: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise _invalid(f"{name} must be {what}", field=name)
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise _invalid(f"{name} must be {what}", field=name) from None
    if not d.is_finite():
        raise _invalid(f"{name} must be {what}", field=name)
    return d


def _int(v: Any, name: str, lo: int = 1) -> int:
    d = _number(v, name, "a positive integer" if lo else "a non-negative integer")
    if d != d.to_integral_value() or d < lo or d > 10_000:
        raise _invalid(f"{name} must be an integer between {lo} and 10000", field=name)
    return int(d)


def _dec(v: Any, name: str, lo: int = 0, hi: int = 100) -> Decimal:
    d = _number(v, name, "a number")
    if d < lo or d > hi:
        raise _invalid(f"{name} must be between {lo} and {hi}", field=name)
    return d


def _norm(name: str, value: Any, default: Any) -> Any:
    kind = _KINDS.get(name)
    if kind == "int0":
        return _int(value, name, lo=0)
    if kind == "signed":
        return _dec(value, name, lo=-100, hi=1000)
    return _dec(value, name) if isinstance(default, Decimal) else _int(value, name)


def _allowed(strategy: str) -> dict[str, Any]:
    return {**_DEFAULTS[strategy], **_OPTIONAL.get(strategy, {}),
            **(_VOLATILITY if strategy in _FILTERED else {})}


def validate_params(strategy: str, params: dict | None) -> dict[str, Any]:
    """Defaults merged with `params`, normalised (ints and Decimals); INVALID_ORDER if bad."""
    if strategy not in _DEFAULTS:
        raise _invalid(f"unknown strategy {strategy!r}", field="strategy",
                       allowed=list(STRATEGIES))
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise _invalid("params must be an object", field="params")
    allowed = _allowed(strategy)
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise _invalid(f"unknown parameters for {strategy}: {unknown}", field="params",
                       allowed=list(allowed))
    out = {**_DEFAULTS[strategy], **params}
    if "atr_period" not in out and ("max_atr_pct" in out or "atr_stop" in out):
        out["atr_period"] = DEFAULT_ATR_PERIOD
    out = {k: _norm(k, out[k], allowed[k]) for k in allowed if k in out}
    if strategy == "sma_cross" and out["fast"] >= out["slow"]:
        raise _invalid("fast must be lower than slow", field="params")
    if strategy == "rsi_reversion" and out["buy_below"] >= out["sell_above"]:
        raise _invalid("buy_below must be lower than sell_above", field="params")
    if strategy == "momentum" and out["exit_below"] > out["enter_above"]:
        raise _invalid("exit_below must not be higher than enter_above", field="params")
    return out


def min_bars(strategy: str, params: dict[str, Any]) -> int:
    """Fewest bars for the strategy to be able to emit its first possible signal."""
    if strategy == "sma_cross":
        need = params["slow"] + 1
    elif strategy == "rsi_reversion":
        need = max(params["period"] + 1, params.get("sma_filter", 0))
    elif strategy == "breakout":
        need = max(params["lookback"], params["exit_lookback"]) + 1
    elif strategy == "momentum":
        need = params["lookback"] + params["skip"] + 1
    elif strategy == "donchian":
        need = max(params["entry"], params["exit"]) + 1
        if params.get("atr_stop"):
            need = max(need, params["atr_period"])
    else:
        return 1
    if params.get("max_atr_pct"):
        need = max(need, params["atr_period"])
    return need


def roc(closes: Sequence[Decimal], lookback: int, skip: int = 0) -> list[Decimal | None]:
    """Rate of change in percent: close[i - skip] / close[i - skip - lookback] - 1, × 100
    (None without enough data or with a non-positive base); 8 decimals."""
    out: list[Decimal | None] = []
    with localcontext() as ctx:
        ctx.prec = 34
        for i in range(len(closes)):
            j = i - skip
            k = j - lookback
            if k < 0 or closes[k] <= 0:
                out.append(None)
            else:
                out.append(((closes[j] / closes[k] - 1) * 100).quantize(
                    _Q, rounding=ROUND_HALF_EVEN))
    return out


def atr_pct(bars: Sequence[Bar], period: int) -> list[Decimal | None]:
    """ATR(period) / close × 100 per bar (None while the ATR warms up); 8 decimals."""
    out: list[Decimal | None] = []
    with localcontext() as ctx:
        ctx.prec = 34
        for b, a in zip(bars, indicators.atr(bars, period), strict=True):
            out.append(None if a is None or b.close <= 0 else
                       (a / b.close * 100).quantize(_Q, rounding=ROUND_HALF_EVEN))
    return out


def atr_stop_line(bars: Sequence[Bar], entry: int, mult: Decimal,
                  period: int) -> list[Decimal | None]:
    """donchian's ATR stop per bar: the highest high of the last `entry` bars (this one
    included) minus mult × ATR(period); None until both are available."""
    atr = indicators.atr(bars, period)
    out: list[Decimal | None] = []
    for i in range(len(bars)):
        if i < entry - 1 or atr[i] is None:
            out.append(None)
        else:
            out.append(max(b.high for b in bars[i - entry + 1 : i + 1]) - mult * atr[i])
    return out


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
    closes = [b.close for b in bars]
    values = indicators.rsi(closes, params["period"])
    n = params.get("sma_filter", 0)
    trend = indicators.sma(closes, n) if n else None
    out: list[Signal] = []
    for i, v in enumerate(values):
        if v is None:
            out.append("hold")
        elif v < params["buy_below"]:
            ok = trend is None or (trend[i] is not None and closes[i] > trend[i])
            out.append("buy" if ok else "hold")
        elif v > params["sell_above"]:
            out.append("sell")
        else:
            out.append("hold")
    return out


def _channel(bars: list[Bar], entry: int, exit_: int,
             stop: list[Decimal | None] | None = None) -> list[Signal]:
    out: list[Signal] = ["hold"] * len(bars)
    for i, bar in enumerate(bars):
        if i >= entry and bar.close > max(b.high for b in bars[i - entry : i]):
            out[i] = "buy"
        else:
            below_channel = i >= exit_ and bar.close < min(b.low for b in bars[i - exit_ : i])
            stopped = stop is not None and stop[i] is not None and bar.close < stop[i]
            if below_channel or stopped:
                out[i] = "sell"
    return out


def breakout(bars: list[Bar], params: dict) -> list[Signal]:
    return _channel(bars, params["lookback"], params["exit_lookback"])


def donchian(bars: list[Bar], params: dict) -> list[Signal]:
    k = params.get("atr_stop", Decimal(0))
    stop = atr_stop_line(bars, params["entry"], k, params["atr_period"]) if k else None
    return _channel(bars, params["entry"], params["exit"], stop)


def momentum(bars: list[Bar], params: dict) -> list[Signal]:
    values = roc([b.close for b in bars], params["lookback"], params["skip"])
    out: list[Signal] = []
    for v in values:
        if v is None:
            out.append("hold")
        elif v > params["enter_above"]:
            out.append("buy")
        elif v < params["exit_below"]:
            out.append("sell")
        else:
            out.append("hold")
    return out


def buy_and_hold(bars: list[Bar], params: dict) -> list[Signal]:
    return ["buy" if i == 0 else "hold" for i in range(len(bars))]


_IMPL: dict[str, Callable[[list[Bar], dict], list[Signal]]] = {
    "sma_cross": sma_cross,
    "rsi_reversion": rsi_reversion,
    "breakout": breakout,
    "momentum": momentum,
    "donchian": donchian,
    "buy_and_hold": buy_and_hold,
}


def volatility_ok(value: Decimal | None, params: dict) -> bool:
    """Whether an entry passes the max_atr_pct filter at an ATR% `value` (off: always)."""
    limit = params.get("max_atr_pct")
    return not limit or (value is not None and value <= limit)


def signals(bars: list[Bar], strategy: str, params: dict | None = None) -> list[Signal]:
    p = validate_params(strategy, params)
    out = _IMPL[strategy](bars, p)
    if p.get("max_atr_pct"):
        pct = atr_pct(bars, p["atr_period"])
        out = ["hold" if s == "buy" and not volatility_ok(pct[i], p) else s
               for i, s in enumerate(out)]
    return out
