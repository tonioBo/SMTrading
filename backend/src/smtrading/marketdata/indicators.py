"""Technical indicators: pure functions returning lists as long as the input.

Positions without enough data are ``None``. All arithmetic runs in a 34-digit
decimal context; only the returned values are quantized (8 decimals, half-even).
"""

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from smtrading.marketdata.types import Bar

_PREC = 34
_Q = Decimal("1E-8")


def _check_period(period: int) -> None:
    if not isinstance(period, int) or isinstance(period, bool) or period < 1:
        raise ValueError(f"period must be a positive integer, got {period!r}")


def _q(x: Decimal | None) -> Decimal | None:
    return None if x is None else x.quantize(_Q, rounding=ROUND_HALF_EVEN)


def _qs(xs: list[Decimal | None]) -> list[Decimal | None]:
    return [_q(x) for x in xs]


def _sma(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    _check_period(period)
    out: list[Decimal | None] = [None] * len(values)
    total = Decimal(0)
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _ema(values: Sequence[Decimal | None], period: int) -> list[Decimal | None]:
    """EMA over a series whose defined part is contiguous (None prefix allowed)."""
    _check_period(period)
    out: list[Decimal | None] = [None] * len(values)
    first = next((i for i, v in enumerate(values) if v is not None), None)
    if first is None or len(values) - first < period:
        return out
    seed_at = first + period - 1
    prev = sum((values[i] for i in range(first, seed_at + 1)), Decimal(0)) / period  # type: ignore[misc]
    out[seed_at] = prev
    alpha = Decimal(2) / (period + 1)
    for i in range(seed_at + 1, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev  # type: ignore[operator]
        out[i] = prev
    return out


def _wilder(
    values: Sequence[Decimal], period: int, start: int, seed: Decimal
) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(values)
    prev = seed
    out[start] = prev
    for i in range(start + 1, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def sma(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    with localcontext() as ctx:
        ctx.prec = _PREC
        return _qs(_sma(values, period))


def ema(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """alpha = 2/(period+1); seeded at index period-1 with the SMA of the first values."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        return _qs(_ema(values, period))


def rsi(values: Sequence[Decimal], period: int = 14) -> list[Decimal | None]:
    """Wilder RSI; first average = simple mean of the first ``period`` changes (index ``period``)."""
    _check_period(period)
    n = len(values)
    out: list[Decimal | None] = [None] * n
    if n <= period:
        return out
    with localcontext() as ctx:
        ctx.prec = _PREC
        gains = [Decimal(0)] * n
        losses = [Decimal(0)] * n
        for i in range(1, n):
            d = values[i] - values[i - 1]
            if d > 0:
                gains[i] = d
            else:
                losses[i] = Decimal(0) - d
        avg_g = sum(gains[1 : period + 1], Decimal(0)) / period
        avg_l = sum(losses[1 : period + 1], Decimal(0)) / period
        for i in range(period, n):
            if i > period:
                avg_g = (avg_g * (period - 1) + gains[i]) / period
                avg_l = (avg_l * (period - 1) + losses[i]) / period
            out[i] = Decimal(100) if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        return _qs(out)


def macd(
    values: Sequence[Decimal], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """Returns (macd, signal, hist). Signal is the EMA of the MACD line from where it is defined."""
    _check_period(fast)
    _check_period(slow)
    _check_period(signal)
    with localcontext() as ctx:
        ctx.prec = _PREC
        ef, es = _ema(values, fast), _ema(values, slow)
        line: list[Decimal | None] = [
            None if a is None or b is None else a - b for a, b in zip(ef, es, strict=True)
        ]
        sig = _ema(line, signal)
        hist: list[Decimal | None] = [
            None if m is None or s is None else m - s for m, s in zip(line, sig, strict=True)
        ]
        return _qs(line), _qs(sig), _qs(hist)


def bollinger(
    values: Sequence[Decimal], period: int = 20, k: Decimal | float | str = 2
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """Returns (mid, upper, lower) using the population standard deviation."""
    _check_period(period)
    kd = Decimal(str(k))
    n = len(values)
    mid: list[Decimal | None] = [None] * n
    up: list[Decimal | None] = [None] * n
    lo: list[Decimal | None] = [None] * n
    with localcontext() as ctx:
        ctx.prec = _PREC
        means = _sma(values, period)
        for i in range(period - 1, n):
            m = means[i]
            window = values[i - period + 1 : i + 1]
            var = sum(((v - m) ** 2 for v in window), Decimal(0)) / period  # type: ignore[operator]
            band = kd * var.sqrt()
            mid[i], up[i], lo[i] = m, m + band, m - band  # type: ignore[operator]
        return _qs(mid), _qs(up), _qs(lo)


def atr(bars: Sequence[Bar], period: int = 14) -> list[Decimal | None]:
    """True Range smoothed with Wilder; TR[0] = high-low; first ATR (index period-1) = mean of TRs."""
    _check_period(period)
    n = len(bars)
    out: list[Decimal | None] = [None] * n
    if n < period:
        return out
    with localcontext() as ctx:
        ctx.prec = _PREC
        tr: list[Decimal] = []
        for i, b in enumerate(bars):
            r = b.high - b.low
            if i > 0:
                pc = bars[i - 1].close
                r = max(r, abs(b.high - pc), abs(b.low - pc))
            tr.append(r)
        seed = sum(tr[:period], Decimal(0)) / period
        return _qs(_wilder(tr, period, period - 1, seed))


def vwap(bars: Sequence[Bar]) -> list[Decimal | None]:
    """Cumulative VWAP over the whole series with typical price (h+l+c)/3."""
    out: list[Decimal | None] = []
    with localcontext() as ctx:
        ctx.prec = _PREC
        pv = vol = Decimal(0)
        for b in bars:
            pv += (b.high + b.low + b.close) / 3 * b.volume
            vol += b.volume
            out.append(pv / vol if vol else None)
        return _qs(out)
