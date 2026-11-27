"""The rule state machine (pure), the same for the live engine and the historical replay.

It never sends orders and has no clock: time comes only from the arguments, so a run is
reproducible. States: `flat` → (`sent`) `pending` → (`entered`) `long` → (`exited`) `flat`;
`canceled` takes a pending entry back to `flat` without a cooldown. The caller's guards
(price age, spread, open market, idempotency) run outside, before each send."""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from smtrading.autorules.indicators import MinuteSeries, SessionVwap
from smtrading.autorules.models import MODELS, MarketState, validate_params
from smtrading.instruments.market_hours import CALENDARS
from smtrading.intraday.calendar import session_window
from smtrading.marketdata.types import Bar

State = Literal["flat", "pending", "long"]
_SERIES_SLACK = 5  # extra bars kept beyond the model's warm-up
SESSION_EXIT_MARGIN_MIN = 2  # the rule exits this long before the worker's day close


@dataclass(frozen=True)
class Entry:
    ts: datetime
    price: Decimal
    values: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Exit:
    ts: datetime
    reason: Literal["time", "session"]


def _to_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding) * tick


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return ts


class RuleRunner:
    """`session_exit_before` = (day_close_minutes + 2) minutes, from the caller: the rule
    exits before the worker's end-of-session safety net."""

    def __init__(
        self, model: str, params: dict, *, market: str, tick_size: Decimal,
        session_exit_before: timedelta,
    ):
        self.params = validate_params(model, params)  # unknown model → INVALID_ORDER
        self.model = MODELS[model]
        self.market = market
        self.tick_size = tick_size
        self.session_exit_before = session_exit_before
        self.warmup = self.model.warmup(self.params)
        self.series = MinuteSeries(self.warmup + _SERIES_SLACK)
        self._vwap = SessionVwap()
        self.state: State = "flat"
        self.filled_at: datetime | None = None
        self.fill_price: Decimal | None = None
        self.last_exit: datetime | None = None
        self._fill_close: datetime | None = None  # close of the session of the fill

    # --- sessions ----------------------------------------------------------------

    @property
    def _has_session(self) -> bool:
        return self.market in CALENDARS  # CRYPTO (and FX): no session windows

    def _session_start(self, ts: datetime) -> datetime | None:
        if not self._has_session:
            return ts.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        window = session_window(self.market, ts)
        return None if window is None else window[0]

    def _roll_session(self, ts: datetime) -> None:
        start = self._session_start(ts)
        if start is not None and start != self._vwap.start:
            self._vwap.reset(start)

    @property
    def vwap(self) -> Decimal | None:
        return self._vwap.value

    # --- inputs ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        """A closed one-minute bar (live or history). Markets with a calendar keep only
        regular-session bars (pre/after-market bars are thin); lookbacks may still span
        earlier sessions."""
        if self._has_session:
            window = session_window(self.market, bar.ts)
            if window is None or not window[0] <= bar.ts < window[1]:
                return
        self._roll_session(bar.ts)
        self.series.add(bar)
        self._vwap.add(bar)

    def on_price(
        self, ts: datetime, price: Decimal, bid: Decimal | None, ask: Decimal | None
    ) -> Entry | Exit | None:
        """The intent at a new price: an Entry (flat), an Exit (long) or None."""
        _aware(ts)
        if self.state == "pending":
            return None
        if self.state == "long":
            return self._exit(ts)
        self._roll_session(ts)
        if self.series.count() < self.warmup or not self._entry_window(ts):
            return None
        if self.last_exit is not None and ts - self.last_exit < timedelta(
            seconds=self.params["cooldown_s"]
        ):
            return None
        values = self.model.entry(MarketState(self.series, self.vwap, self._vwap.start), price, self.params)
        return None if values is None else Entry(ts, price, values)

    def _entry_window(self, ts: datetime) -> bool:
        if not self._has_session:
            return True
        window = session_window(self.market, ts)
        if window is None:
            return False
        open_, close = window
        first = timedelta(minutes=self.params["no_entry_first_min"])
        last = max(timedelta(minutes=self.params["no_entry_last_min"]), self.session_exit_before)
        return open_ + first <= ts < close - last

    def _exit(self, ts: datetime) -> Exit | None:
        if self._has_session:
            close = self._fill_close
            if close is None:
                window = session_window(self.market, ts)
                close = None if window is None else window[1]
            if close is not None and ts >= close - self.session_exit_before:
                return Exit(ts, "session")
        if self.filled_at is not None and ts - self.filled_at >= timedelta(
            seconds=self.params["max_hold_s"]
        ):
            return Exit(ts, "time")
        return None

    # --- state updates from the caller ------------------------------------------

    def sent(self, ts: datetime) -> None:
        _aware(ts)
        self.state = "pending"

    def entered(self, ts: datetime, fill_price: Decimal) -> None:
        _aware(ts)
        self.state = "long"
        self.filled_at = ts
        self.fill_price = fill_price
        window = session_window(self.market, ts) if self._has_session else None
        self._fill_close = None if window is None else window[1]

    def exited(self, ts: datetime) -> None:
        _aware(ts)
        self.state = "flat"
        self.last_exit = ts
        self.filled_at = self.fill_price = self._fill_close = None

    def canceled(self) -> None:
        """The entry was not filled: back to flat, no cooldown."""
        self.state = "flat"

    def levels(self, expected: Decimal) -> tuple[Decimal, Decimal]:
        """(stop rounded down to the tick, target rounded up to the tick)."""
        stop = expected * (1 - self.params["stop_pct"] / 100)
        target = expected * (1 + self.params["target_pct"] / 100)
        return (_to_tick(stop, self.tick_size, ROUND_FLOOR),
                _to_tick(target, self.tick_size, ROUND_CEILING))
