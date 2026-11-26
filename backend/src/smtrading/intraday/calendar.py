"""The intraday day calendar (pure): when the bars of a session close, the safety-close
cutoff and the 5-session window. Markets without a calendar (CRYPTO, FX) have no day."""
from datetime import UTC, date, datetime, timedelta

from smtrading.instruments.market_hours import CALENDARS, _calendar, local_date, session_on
from smtrading.marketdata.bar_close import TIMEFRAME_DURATION
from smtrading.marketdata.types import Timeframe

BAR_DELAY = timedelta(seconds=60)  # a closed bar is available from the provider after this
_MAX_LOOKBACK_DAYS = 30  # calendar days scanned for sessions (holidays, closures)


def _session(market: str, day: date) -> tuple[datetime, datetime, bool] | None:
    if market not in CALENDARS:
        return None
    return session_on(market, day)


def session_window(market: str, now: datetime) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC of the session of the local date of `now`; None without a
    session or for a market without a calendar."""
    if market not in CALENDARS:
        return None
    s = _session(market, local_date(market, now))
    return None if s is None else (s[0], s[1])


def bar_closes(market: str, tf: Timeframe, day: date) -> list[datetime]:
    """The closes (UTC) of the clock-aligned `tf` bars of the session of `day`: from the
    first multiple of the step strictly after the open, up to the session close."""
    s = _session(market, day)
    if s is None:
        return []
    open_, close, _ = s
    tz = _calendar(CALENDARS[market]).tz
    step = TIMEFRAME_DURATION[tf]
    local_open = open_.astimezone(tz)
    midnight = local_open.replace(hour=0, minute=0, second=0, microsecond=0)
    k = (local_open - midnight) // step + 1
    out: list[datetime] = []
    t = midnight + k * step  # local wall-clock arithmetic: DST-correct
    while t.astimezone(UTC) <= close:
        out.append(t.astimezone(UTC))
        t += step
    return out


def due_bar_close(
    market: str, tf: Timeframe, now: datetime, delay: timedelta = BAR_DELAY
) -> datetime | None:
    """The latest close `c` of today's session with `c + delay <= now < session close`;
    None when the market is closed, on holidays or before the first close."""
    window = session_window(market, now)
    if window is None or not (window[0] <= now < window[1]):
        return None
    due = [c for c in bar_closes(market, tf, local_date(market, now)) if c + delay <= now]
    return due[-1] if due else None


def day_close_at(market: str, now: datetime, minutes: int) -> datetime | None:
    """The close of today's session minus `minutes`; None without a session."""
    window = session_window(market, now)
    return None if window is None else window[1] - timedelta(minutes=minutes)


def last_sessions(market: str, now: datetime, n: int = 5) -> list[date]:
    """The last `n` session dates up to today included (when today is a session),
    oldest first."""
    if market not in CALENDARS:
        return []
    out: list[date] = []
    day = local_date(market, now)
    for _ in range(_MAX_LOOKBACK_DAYS):
        if len(out) == n:
            break
        if _session(market, day) is not None:
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


def session_after(market: str, day: date, n: int) -> date:
    """The `n`-th session after `day`."""
    seen = 0
    d = day
    for _ in range(_MAX_LOOKBACK_DAYS * max(n, 1)):
        d += timedelta(days=1)
        if _session(market, d) is not None:
            seen += 1
            if seen == n:
                return d
    raise ValueError(f"no session {n} after {day} for {market}")
