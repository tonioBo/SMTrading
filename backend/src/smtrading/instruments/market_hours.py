"""Market hours: exchange calendars (`exchange_calendars`) plus the manual closures of
the admin (`market_closures`, loaded into this process's registry by
`instruments.closures`), CRYPTO always open, FX by the weekly switch.

A manual closure only closes (a whole day, or from an earlier time): it never opens a
calendar holiday. Its day is the exchange's local date (`cal.tz`), its time local too."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache

import exchange_calendars as xcals
import pandas as pd

from smtrading.common.errors import ErrorCode, SMTError

CALENDARS = {
    "NASDAQ": "XNAS", "NYSE": "XNYS", "MTA": "XMIL", "XETRA": "XETR", "EURONEXT_PARIS": "XPAR",
    "LSE": "XLON",
}
# the tradable markets (catalog, instrument creation, discovery)
MARKETS = ("NASDAQ", "NYSE", "MTA", "XETRA", "EURONEXT_PARIS", "CRYPTO", "FX")
# hours and holidays only: London stocks stay non-tradable (user decision, plan 9)
HOURS_ONLY_MARKETS = ("LSE",)
STATUS_MARKETS = MARKETS + HOURS_ONLY_MARKETS
_FX_SWITCH_HOUR = 22  # UTC: weekly open Sunday 22:00, close Friday 22:00
_MAX_SKIPPED_DAYS = 10  # manually closed sessions skipped looking for the next open
CLOSED, EARLY_CLOSE = "closed", "early_close"


@dataclass(frozen=True)
class Closure:
    market: str
    day: date  # the exchange's local date
    kind: str  # CLOSED | EARLY_CLOSE
    close_time: time | None  # local time of the exchange, EARLY_CLOSE only


_closures: dict[tuple[str, date], Closure] = {}


def set_closures(rows: Iterable[Closure]) -> None:
    """Replaces the whole registry of this process (one reference swap)."""
    global _closures
    _closures = {(c.market, c.day): c for c in rows}


@dataclass(frozen=True)
class MarketStatus:
    market: str
    is_open: bool
    next_open: datetime | None
    next_close: datetime | None


@cache
def _calendar(code: str):
    return xcals.get_calendar(code)


def _to_py(ts: pd.Timestamp) -> datetime:
    return ts.to_pydatetime().astimezone(UTC)


def _local_day(cal, ts: pd.Timestamp) -> date:
    return ts.tz_convert(cal.tz).date()


def _manual_close(market: str, cal, session_open: pd.Timestamp) -> pd.Timestamp | None:
    """The manual end of the session opening at `session_open` (its open itself when the
    day is closed), or None when no closure applies to that day."""
    closure = _closures.get((market, _local_day(cal, session_open)))
    if closure is None:
        return None
    if closure.kind == EARLY_CLOSE and closure.close_time is not None:
        local = datetime.combine(_local_day(cal, session_open), closure.close_time,
                                 tzinfo=cal.tz)
        return max(pd.Timestamp(local).tz_convert(UTC), session_open)
    return session_open


def _session_close(market: str, cal, session_open: pd.Timestamp) -> pd.Timestamp:
    close = cal.next_close(session_open)
    manual = _manual_close(market, cal, session_open)
    return close if manual is None else min(close, manual)


def _exchange_status(market: str, at: datetime) -> MarketStatus:
    cal = _calendar(CALENDARS[market])
    ts = pd.Timestamp(at).floor("min")  # calendars work at minute resolution
    if cal.is_open_on_minute(ts, ignore_breaks=True):
        session_open = cal.previous_open(ts + pd.Timedelta(minutes=1))
        close = _session_close(market, cal, session_open)
        if ts < close:
            return MarketStatus(market, True, None, _to_py(close))
    next_open = cal.next_open(ts)
    for _ in range(_MAX_SKIPPED_DAYS):
        if _session_close(market, cal, next_open) > next_open:
            break
        next_open = cal.next_open(next_open)  # a manually closed day: the next session
    return MarketStatus(market, False, _to_py(next_open),
                        _to_py(_session_close(market, cal, next_open)))


def _fx_status(at: datetime) -> MarketStatus:
    at = at.astimezone(UTC)
    anchor = at.replace(hour=_FX_SWITCH_HOUR, minute=0, second=0, microsecond=0)
    # Most recent Sunday 22:00 UTC (the weekly open) at or before `at`.
    week_open = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
    if week_open > at:
        week_open -= timedelta(days=7)
    week_close = week_open + timedelta(days=5)  # Friday 22:00 UTC
    if at < week_close:
        return MarketStatus("FX", True, None, week_close)
    next_open = week_open + timedelta(days=7)
    return MarketStatus("FX", False, next_open, next_open + timedelta(days=5))


def market_status(market: str, at: datetime) -> MarketStatus:
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if market in CALENDARS:
        return _exchange_status(market, at)
    if market == "CRYPTO":
        return MarketStatus("CRYPTO", True, None, None)
    if market == "FX":
        return _fx_status(at)
    raise SMTError(ErrorCode.NOT_FOUND, f"unknown market {market!r}", {"market": market})


def all_market_status(at: datetime) -> list[MarketStatus]:
    return [market_status(m, at) for m in STATUS_MARKETS]


def previous_session_open(market: str, at: datetime) -> datetime:
    """«One trading day» back: the open of the session before the last one started at or
    before `at` (calendar holidays and manually closed days skipped). CRYPTO: `at` - 24 h;
    FX: `at` - 24 h, while closed the weekly close - 24 h."""
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if market == "CRYPTO":
        return at - timedelta(hours=24)
    if market == "FX":
        status = _fx_status(at)
        if status.is_open:
            return at - timedelta(hours=24)
        week_close = status.next_open - timedelta(days=2)  # Sunday 22:00 -> Friday 22:00
        return week_close - timedelta(hours=24)
    if market not in CALENDARS:
        raise SMTError(ErrorCode.NOT_FOUND, f"unknown market {market!r}", {"market": market})
    cal = _calendar(CALENDARS[market])
    ts = pd.Timestamp(at).floor("min")
    found: list[pd.Timestamp] = []
    session_open = cal.previous_open(ts + pd.Timedelta(minutes=1))  # started at or before
    for _ in range(2 * _MAX_SKIPPED_DAYS):
        if _session_close(market, cal, session_open) > session_open:
            found.append(session_open)
            if len(found) == 2:
                return _to_py(found[1])
        session_open = cal.previous_open(session_open)
    raise SMTError(ErrorCode.NOT_FOUND, f"no previous session for {market!r}",
                   {"market": market})
