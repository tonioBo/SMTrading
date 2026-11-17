"""The periods of a goal (pure, local dates of `get_settings().tz`).

- week: Monday to Sunday; month; quarter (Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec); year;
  each the calendar period containing a reference date;
- custom: `start_date` to `end_date`, both included.
A recurring goal's current period is the one containing today (from its first period on:
before `start_date` it is the first period, not started yet), so it rolls over at local
midnight by itself. A goal that does not recur has one period only: the one containing
`start_date`.

The elapsed time of a period is measured in business days: a day is one when one of the
main markets (NYSE, NASDAQ, MTA, XETRA) trades, manual closures included; outside the
calendars' window, Monday to Friday."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from smtrading.instruments.market_hours import session_on

MAIN_MARKETS = ("NYSE", "NASDAQ", "MTA", "XETRA")
_Q2 = Decimal("0.01")


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # included


def calendar_period(horizon: str, day: date) -> Period:
    """The calendar period of `horizon` (week, month, quarter, year) containing `day`."""
    if horizon == "week":
        monday = day - timedelta(days=day.weekday())
        return Period(monday, monday + timedelta(days=6))
    if horizon == "month":
        first = day.replace(day=1)
    elif horizon == "quarter":
        first = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    elif horizon == "year":
        first = date(day.year, 1, 1)
    else:
        raise ValueError(f"no calendar period for {horizon!r}")
    months = {"month": 1, "quarter": 3, "year": 12}[horizon]
    year, month = divmod(first.month - 1 + months, 12)
    return Period(first, date(first.year + year, month + 1, 1) - timedelta(days=1))


def goal_period(horizon: str, start_date: date, end_date: date | None, recurring: bool,
                today: date) -> Period:
    """The period of a goal shown today: the current one of a recurring goal (its first
    while `today` is before it), else its only one."""
    if horizon == "custom":
        if end_date is None:
            raise ValueError("a custom goal needs end_date")
        return Period(start_date, end_date)
    ref = max(today, start_date) if recurring else start_date
    return calendar_period(horizon, ref)


def previous_period(horizon: str, period: Period) -> Period:
    """The period of `horizon` right before `period` (calendar horizons)."""
    return calendar_period(horizon, period.start - timedelta(days=1))


def is_business_day(day: date) -> bool:
    try:
        return any(session_on(m, day) is not None for m in MAIN_MARKETS)
    except ValueError:  # outside the calendars' window
        return day.weekday() < 5


def business_days(start: date, end: date) -> int:
    """Business days in [start, end], both included (0 when end < start)."""
    out = 0
    day = start
    while day <= end:
        out += is_business_day(day)
        day += timedelta(days=1)
    return out


def elapsed_pct(period: Period, now_local: datetime) -> Decimal:
    """The share of the period's business days elapsed at `now_local`, in percent (0 to
    100, 2 decimals): the whole business days before today plus the part of today gone
    by, when today is one. A period without business days counts by its calendar days."""
    today = now_local.date()
    if today < period.start:
        return Decimal(0)
    if today > period.end:
        return Decimal(100)
    total = business_days(period.start, period.end)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    part = Decimal((now_local - midnight).total_seconds()) / Decimal(86400)
    if total == 0:
        total = (period.end - period.start).days + 1
        done = Decimal((today - period.start).days) + part
    else:
        done = Decimal(business_days(period.start, today - timedelta(days=1)))
        if is_business_day(today):
            done += part
    return min(Decimal(100), done * 100 / total).quantize(_Q2, rounding=ROUND_HALF_EVEN)
