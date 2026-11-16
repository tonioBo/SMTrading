from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from typing import Any, Literal

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata import indicators as ind
from smtrading.marketdata.types import Bar, Timeframe

IndicatorName = Literal["sma", "ema", "rsi", "macd", "bollinger", "atr", "vwap"]

# Approximate wall-clock span per bar, widened to cover weekends / closed hours.
_BAR_SPAN: dict[Timeframe, timedelta] = {
    Timeframe.m1: timedelta(minutes=1) * 3,
    Timeframe.m5: timedelta(minutes=5) * 3,
    Timeframe.m15: timedelta(minutes=15) * 3,
    Timeframe.h1: timedelta(hours=1) * 3,
    Timeframe.d1: timedelta(days=1) * 1.6,
    Timeframe.w1: timedelta(weeks=1),
}
_DEFAULT_PERIOD = {"sma": 20, "ema": 20, "rsi": 14, "atr": 14, "bollinger": 20}


@dataclass
class IndicatorSpec:
    name: IndicatorName
    params: dict = field(default_factory=dict)


def _invalid(message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, details or None)


def _max_period(spec: IndicatorSpec) -> int:
    p = spec.params
    if spec.name == "macd":
        return int(p.get("slow", 26)) + int(p.get("signal", 9))
    if spec.name == "vwap":
        return 0
    return int(p.get("period", _DEFAULT_PERIOD[spec.name]))


def _run(spec: IndicatorSpec, bars: list) -> Any:
    closes = [b.close for b in bars]
    fns: dict[str, Callable[..., Any]] = {
        "sma": lambda **p: ind.sma(closes, **p),
        "ema": lambda **p: ind.ema(closes, **p),
        "rsi": lambda **p: ind.rsi(closes, **p),
        "macd": lambda **p: dict(
            zip(("macd", "signal", "hist"), ind.macd(closes, **p), strict=True)
        ),
        "bollinger": lambda **p: dict(
            zip(("mid", "upper", "lower"), ind.bollinger(closes, **p), strict=True)
        ),
        "atr": lambda **p: ind.atr(bars, **p),
        "vwap": lambda **p: ind.vwap(bars, **p),
    }
    return fns[spec.name](**spec.params)


def spec_key(spec: IndicatorSpec) -> str:
    """`<name>_<param values joined by _>` (just the name without params)."""
    return "_".join([str(spec.name), *(str(v) for v in spec.params.values())])


def _result_keys(specs: list[IndicatorSpec]) -> list[str]:
    """The name, or `spec_key` when the same name appears more than once (then `_2`, ...
    for identical specs)."""
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.name] = counts.get(spec.name, 0) + 1
    keys: list[str] = []
    for spec in specs:
        key = spec.name if counts[spec.name] == 1 else spec_key(spec)
        base, n = key, 1
        while key in keys:
            n += 1
            key = f"{base}_{n}"
        keys.append(key)
    return keys


def _tail(result: Any, lookback: int) -> Any:
    if isinstance(result, dict):
        return {k: v[-lookback:] for k, v in result.items()}
    return result[-lookback:]


def _check_specs(specs: list[IndicatorSpec], lookback: int) -> None:
    if lookback < 1:
        raise _invalid("lookback must be positive", lookback=lookback)
    for spec in specs:
        if spec.name not in _DEFAULT_PERIOD and spec.name not in ("macd", "vwap"):
            raise _invalid("unknown indicator", name=str(spec.name))


async def fetch_bars(
    router: Any, symbol: str, timeframe: Timeframe, n_bars: int, now: datetime | None = None
) -> list[Bar]:
    """The last `n_bars` closed bars (fewer when the history is shorter), oldest first."""
    try:
        tf = Timeframe(timeframe)
        end = now or datetime.now(UTC)
        start = end - _BAR_SPAN[tf] * n_bars
    except (ValueError, TypeError, OverflowError) as exc:
        raise _invalid("bad timeframe or parameters") from exc
    bars = await router.get_bars(symbol, tf, start, end)
    return list(bars)[-n_bars:]


async def indicator_bars(
    router: Any, symbol: str, timeframe: Timeframe, specs: list[IndicatorSpec],
    lookback: int = 200,
) -> list[Bar]:
    """The closed bars `compute_indicators` needs: `lookback` plus a warm-up of three
    times the longest period."""
    _check_specs(specs, lookback)
    try:
        max_period = max((_max_period(s) for s in specs), default=0)
        n_bars = lookback + max_period * 3
    except (ValueError, TypeError, OverflowError) as exc:
        raise _invalid("bad timeframe or parameters") from exc
    return await fetch_bars(router, symbol, timeframe, n_bars)


def indicators_from_bars(
    bars: list[Bar], symbol: str, timeframe: Timeframe, specs: list[IndicatorSpec],
    lookback: int = 200,
) -> dict:
    """`compute_indicators` over the given bars (oldest first)."""
    _check_specs(specs, lookback)
    keys = _result_keys(specs)
    out: dict = {
        "symbol": symbol,
        "timeframe": Timeframe(timeframe).value,
        "ts": [b.ts for b in bars][-lookback:],
        "specs": [
            {"key": key, "name": spec.name, "params": spec.params}
            for key, spec in zip(keys, specs, strict=True)
        ],
    }
    for key, spec in zip(keys, specs, strict=True):
        try:
            out[key] = _tail(_run(spec, bars), lookback)
        except (TypeError, ValueError, InvalidOperation, OverflowError) as exc:
            raise _invalid("bad indicator parameters", name=spec.name, params=spec.params) from exc
    return out


async def compute_indicators(
    router: Any,
    symbol: str,
    timeframe: Timeframe,
    specs: list[IndicatorSpec],
    lookback: int = 200,
) -> dict:
    """{symbol, timeframe, ts, specs: [{key, name, params}], <key>: values...}: one result
    per spec under its name, or under `spec_key` when a name is requested more than once.
    Huge or invalid parameters are INVALID_ORDER."""
    bars = await indicator_bars(router, symbol, timeframe, specs, lookback)
    return indicators_from_bars(bars, symbol, timeframe, specs, lookback)
