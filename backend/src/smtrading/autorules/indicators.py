"""Pure one-minute indicators of the automatic rules (Decimal only, no I/O)."""
from collections import deque
from datetime import datetime
from decimal import Context, Decimal

from smtrading.marketdata.types import Bar

_SQRT_CTX = Context(prec=28)


class MinuteSeries:
    """The latest closed one-minute bars, oldest first, at most `maxlen`."""

    def __init__(self, maxlen: int):
        self._bars: deque[Bar] = deque(maxlen=max(maxlen, 1))

    def add(self, bar: Bar) -> None:
        """Append a closed bar; one with the last bar's `ts` replaces it (a corrected bar),
        an older one is ignored."""
        if self._bars:
            last = self._bars[-1].ts
            if bar.ts == last:
                self._bars[-1] = bar
                return
            if bar.ts < last:
                return
        self._bars.append(bar)

    def count(self) -> int:
        return len(self._bars)

    def closed(self, n: int) -> list[Bar]:
        """The last `n` bars (fewer when the series is shorter)."""
        if n <= 0:
            return []
        return list(self._bars)[-n:]

    def _last(self, n: int) -> list[Bar] | None:
        return self.closed(n) if 0 < n <= len(self._bars) else None

    def highest(self, n: int) -> Decimal | None:
        bars = self._last(n)
        return None if bars is None else max(b.high for b in bars)

    def mean_std(self, n: int) -> tuple[Decimal, Decimal] | None:
        """Mean and population standard deviation of the last `n` closes."""
        bars = self._last(n)
        if bars is None:
            return None
        closes = [b.close for b in bars]
        mean = sum(closes, Decimal(0)) / n
        var = sum(((c - mean) ** 2 for c in closes), Decimal(0)) / n
        return mean, var.sqrt(_SQRT_CTX)

    def avg_volume(self, n: int) -> Decimal | None:
        bars = self._last(n)
        return None if bars is None else sum((b.volume for b in bars), Decimal(0)) / n


class SessionVwap:
    """The session VWAP from one-minute bars: Σ(typical price × volume) / Σ volume, with
    typical price (high + low + close) / 3 (the same live and on history)."""

    def __init__(self) -> None:
        self.start: datetime | None = None
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._last: tuple[datetime, Decimal, Decimal] | None = None  # ts, pv, v

    def reset(self, start: datetime) -> None:
        self.start = start
        self._pv = Decimal(0)
        self._v = Decimal(0)
        self._last = None

    def add(self, bar: Bar) -> None:
        """Like MinuteSeries.add: a bar with the last bar's `ts` replaces its contribution
        (a corrected minute), an older one is ignored; bars before `start` are ignored."""
        if self.start is None or bar.ts < self.start:
            return
        pv = (bar.high + bar.low + bar.close) * bar.volume  # / 3 once, in value
        if self._last is not None:
            last_ts, last_pv, last_v = self._last
            if bar.ts < last_ts:
                return
            if bar.ts == last_ts:
                self._pv -= last_pv
                self._v -= last_v
        self._pv += pv
        self._v += bar.volume
        self._last = (bar.ts, pv, bar.volume)

    @property
    def value(self) -> Decimal | None:
        return None if self._v == 0 else self._pv / (3 * self._v)
