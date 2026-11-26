"""The end-of-day summary of mode A (Telegram, `source="system"`): the triggers of a
portfolio's session by outcome, and its own day trades in the window."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.market_hours import CALENDARS, _calendar, session_on
from smtrading.intraday.day_trades import DAY_TRADE_MARKET, own_cap_usage
from smtrading.intraday.models import (
    CAPPED,
    FAILED,
    ISSUE_OPEN,
    LATE,
    NO_AGENT,
    PENDING,
    TELEGRAM,
    WOKEN,
    IntradayTrigger,
)
from smtrading.portfolios.models import Portfolio


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """The local day of DAY_TRADE_MARKET in UTC: the summary's session day."""
    tz = _calendar(CALENDARS[DAY_TRADE_MARKET]).tz
    start = datetime.combine(day, time(0), tzinfo=tz)
    return start, datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)


async def day_summary_text(session: AsyncSession, portfolio: Portfolio, day: date) -> str | None:
    """«Riepilogo di giornata · …» of the triggers whose bar closed on `day` (local date of
    DAY_TRADE_MARKET); None without triggers. The seven counts add up to the total: a
    trigger still `pending` counts as failed."""
    start, end = _day_bounds(day)
    rows = await session.execute(
        select(IntradayTrigger.outcome, func.count())
        .where(IntradayTrigger.portfolio_id == portfolio.id,
               IntradayTrigger.bar_close >= start, IntradayTrigger.bar_close < end)
        .group_by(IntradayTrigger.outcome))
    by = dict(rows.all())
    n = sum(by.values())
    if not n:
        return None
    failed = by.get(FAILED, 0) + by.get(PENDING, 0)
    session_ = session_on(DAY_TRADE_MARKET, day)
    usage = await own_cap_usage(session, portfolio, session_[1] if session_ else end)
    used = str(usage.used)
    if usage.limit is not None:
        used = f"{usage.used}/{usage.limit}"
        if usage.frees_on is not None:
            used += f", la prossima si libera il {usage.frees_on:%d/%m}"
    return (
        f"Riepilogo di giornata · {portfolio.name} · {day:%d/%m}: {n} scatti — "
        f"{by.get(WOKEN, 0)} sveglie, {by.get(TELEGRAM, 0)} solo Telegram, "
        f"{by.get(CAPPED, 0)} saltati per tetto, {by.get(ISSUE_OPEN, 0)} con compito già "
        f"aperto, {by.get(NO_AGENT, 0)} senza agente, {by.get(LATE, 0)} dopo l'orario, "
        f"{failed} non riusciti; operazioni di giornata {used}")
