from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from typing import Any, Literal

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.marketdata import indicators as ind
from smtrading.marketdata.types import Timeframe

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


def _tail(result: Any, lookback: int) -> Any:
    if isinstance(result, dict):
        return {k: v[-lookback:] for k, v in result.items()}
    return result[-lookback:]


async def compute_indicators(
    router: Any,
    symbol: str,
    timeframe: Timeframe,
    specs: list[IndicatorSpec],
    lookback: int = 200,
) -> dict:
    if lookback < 1:
        raise _invalid("lookback must be positive", lookback=lookback)
    for spec in specs:
        if spec.name not in _DEFAULT_PERIOD and spec.name not in ("macd", "vwap"):
            raise _invalid("unknown indicator", name=str(spec.name))
    try:
        tf = Timeframe(timeframe)
        max_period = max((_max_period(s) for s in specs), default=0)
    except (ValueError, TypeError) as exc:
        raise _invalid("bad timeframe or parameters") from exc
    n_bars = lookback + max_period * 3
    end = datetime.now(UTC)
    start = end - _BAR_SPAN[tf] * n_bars
    bars = await router.get_bars(symbol, tf, start, end)
    bars = list(bars)[-n_bars:]

    out: dict = {
        "symbol": symbol,
        "timeframe": tf.value,
        "ts": [b.ts for b in bars][-lookback:],
    }
    for spec in specs:
        try:
            out[spec.name] = _tail(_run(spec, bars), lookback)
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise _invalid("bad indicator parameters", name=spec.name, params=spec.params) from exc
    return out
