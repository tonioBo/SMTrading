"""When a daily or weekly bar is closed, and until when a cached bars list holds.

Providers stamp 1d/1wk bars with the session date (Monday for 1wk) at 00:00 UTC, so the
old rule «ts + duration <= now» kept today's bar out until midnight UTC (01:00 Rome),
hours after the exchange closed. For an exchange-traded instrument a 1d bar is closed
once that exchange's regular session of the bar's local date has closed (calendar early
closes and the admin's manual closures included: `market_hours.session_on`), a 1wk bar
once the week's last session has; plus CLOSE_GRACE for the final print to reach the
providers (EU data can be 15 minutes late). CRYPTO, FX, intraday bars and bars dated a
day without a session keep the duration rule.

A bars list cached at T is final up to T: `bars_cache_ttl` caps the cache entry so it
expires when the next bar becomes closed (the next session close + grace for exchanges,
the next 00:00 UTC for CRYPTO/FX), so no list without today's bar is served after the
close.
"""

import math
from datetime import UTC, datetime, timedelta

from smtrading.instruments.market_hours import (
    CALENDARS,
    local_date,
    market_status,
    session_on,
)
from smtrading.marketdata.cache import bars_ttl
from smtrading.marketdata.types import Timeframe

SESSION_TIMEFRAMES = frozenset({Timeframe.d1, Timeframe.w1})
_EU_MARKETS = frozenset({"MTA", "XETRA", "EURONEXT_PARIS", "LSE"})
CLOSE_GRACE_US = timedelta(minutes=10)
CLOSE_GRACE_EU = timedelta(minutes=20)  # quotes and closes can come 15 minutes late
_DURATION = {
    Timeframe.m1: timedelta(minutes=1),
    Timeframe.m5: timedelta(minutes=5),
    Timeframe.m15: timedelta(minutes=15),
    Timeframe.h1: timedelta(hours=1),
    Timeframe.d1: timedelta(days=1),
    Timeframe.w1: timedelta(weeks=1),
}


def close_grace(market: str) -> timedelta:
    return CLOSE_GRACE_EU if market in _EU_MARKETS else CLOSE_GRACE_US


def _sessions(market: str | None, tf: Timeframe) -> bool:
    return market in CALENDARS and tf in SESSION_TIMEFRAMES


def bar_close_time(market: str | None, tf: Timeframe, ts: datetime) -> datetime | None:
    """When the session bar starting at `ts` is closed (grace included); None when the
    duration rule applies."""
    if not _sessions(market, tf):
        return None
    days = 1 if tf == Timeframe.d1 else 7
    # the nearest UTC date: a stamp at the exchange's local midnight (Europe: 23:00 UTC
    # the day before; New York: 05:00 UTC) still names its session
    day = (ts.astimezone(UTC) + timedelta(hours=12)).date()
    for offset in range(days - 1, -1, -1):  # the period's last session
        session = session_on(market, day + timedelta(days=offset))  # type: ignore[arg-type]
        if session is not None:
            return session[1] + close_grace(market)  # type: ignore[arg-type]
    return None


def bar_is_closed(market: str | None, tf: Timeframe, ts: datetime, now: datetime) -> bool:
    close = bar_close_time(market, tf, ts)
    if close is None:
        return ts + _DURATION[tf] <= now
    return close <= now


def _next_bar_close(market: str, now: datetime) -> datetime:
    grace = close_grace(market)
    today = session_on(market, local_date(market, now))
    if today is not None and today[1] + grace > now:
        return today[1] + grace
    status = market_status(market, now)
    if status.next_close is None or status.next_close + grace <= now:
        return now + timedelta(days=1)
    return status.next_close + grace


def bars_cache_ttl(market: str | None, tf: Timeframe, now: datetime) -> int:
    """The bars cache TTL: the timeframe's, capped for 1d/1wk at the next moment a bar
    becomes closed (at least 1 s)."""
    ttl = bars_ttl(tf)
    if tf not in SESSION_TIMEFRAMES:
        return ttl
    if _sessions(market, tf):
        until = _next_bar_close(market, now)  # type: ignore[arg-type]
    else:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        until = midnight + timedelta(days=1)
    return max(1, min(ttl, math.ceil((until - now).total_seconds())))
