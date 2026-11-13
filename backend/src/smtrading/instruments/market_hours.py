from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import exchange_calendars as xcals
import pandas as pd

from smtrading.common.errors import ErrorCode, SMTError

CALENDARS = {
    "NASDAQ": "XNAS", "NYSE": "XNYS", "MTA": "XMIL", "XETRA": "XETR", "EURONEXT_PARIS": "XPAR",
}
MARKETS = ("NASDAQ", "NYSE", "MTA", "XETRA", "EURONEXT_PARIS", "CRYPTO", "FX")
_FX_SWITCH_HOUR = 22  # UTC: weekly open Sunday 22:00, close Friday 22:00


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


def _exchange_status(market: str, at: datetime) -> MarketStatus:
    cal = _calendar(CALENDARS[market])
    ts = pd.Timestamp(at).floor("min")  # calendars work at minute resolution
    if cal.is_open_on_minute(ts, ignore_breaks=True):
        return MarketStatus(market, True, None, _to_py(cal.next_close(ts)))
    next_open = cal.next_open(ts)
    return MarketStatus(market, False, _to_py(next_open), _to_py(cal.next_close(next_open)))


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
    return [market_status(m, at) for m in MARKETS]
