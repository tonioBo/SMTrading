"""Quiet hours: 23:00 <= local time < 07:00 (pure)."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

QUIET_START = time(23)
QUIET_END = time(7)


def is_quiet(now: datetime, tz: str) -> bool:
    local = now.astimezone(ZoneInfo(tz)).time()
    return local >= QUIET_START or local < QUIET_END


def next_quiet_end(now: datetime, tz: str) -> datetime:
    """The next 07:00 local time strictly after `now`, in UTC (DST aware)."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    day = local.date()
    if local.time() >= QUIET_END:
        day += timedelta(days=1)
    return datetime.combine(day, QUIET_END, tzinfo=zone).astimezone(UTC)
