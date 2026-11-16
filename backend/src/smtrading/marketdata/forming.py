"""The bar still forming («se chiudesse ora»): today's (1d) or this week's (1wk) bar built
from the current quote, to read an indicator as if the market closed now.

Only while the symbol's market is open (CRYPTO always; holidays, weekends and the admin's
manual closures are closed; FX not before Monday 00:00 UTC) and with a quote that is
fresh (no older than the asset class's stale_quote_s) and from today's session. The bar
carries the period's date at 00:00 UTC like the providers' closed bars (exchange local
date; UTC for CRYPTO/FX, Monday for 1wk); open = the previous close (the period's first price is not fetched: a cheap
approximation), high/low = max/min of open and the price, close = the quote's last (mid
when it has none), volume 0. Intraday timeframes are never extended.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import CALENDARS, local_date, market_status, session_on
from smtrading.marketdata.types import Bar, Quote, Timeframe

FORMING_TIMEFRAMES = frozenset({Timeframe.d1, Timeframe.w1})

NOT_SUPPORTED = "timeframe not supported"
MARKET_CLOSED = "market closed"
NO_QUOTE = "no quote"
STALE_QUOTE = "stale quote"
BAR_CLOSED = "bar already closed"


@dataclass(frozen=True)
class Forming:
    bar: Bar | None  # None: nothing to add (see `skipped`)
    price: Decimal | None  # the quote's price (None without a quote)
    quote_ts: datetime | None
    skipped: str | None  # why there is no bar


def quote_price(q: Quote) -> Decimal | None:
    """The last trade, else the mid of bid/ask; None when neither is there."""
    if q.last is not None:
        return q.last
    if q.bid is not None and q.ask is not None:
        return (q.bid + q.ask) / 2
    return None


def period_start(market: str, tf: Timeframe, at: datetime) -> datetime:
    """00:00 UTC of the period's date: the local session date (1d) or its Monday (1wk)."""
    day = local_date(market, at)
    if tf == Timeframe.w1:
        day -= timedelta(days=day.weekday())
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def forming_bar(
    router: Any, instrument: Any, tf: Timeframe, closed: list[Bar], stale_s: int,
    now: datetime | None = None,
) -> Forming:
    """The forming bar of `instrument` after the `closed` bars (oldest first), or why not.

    The quote is read even when no bar can be built, so callers still get the price."""
    now = now or datetime.now(UTC)
    if tf not in FORMING_TIMEFRAMES:
        return Forming(None, None, None, NOT_SUPPORTED)
    symbol, market = instrument.symbol, instrument.market
    try:
        quote = (await router.get_quotes([symbol]))[symbol]
    except (SMTError, KeyError):
        quote = None
    price = quote_price(quote) if quote is not None else None
    ts = quote.ts if quote is not None else None

    def skip(reason: str) -> Forming:
        return Forming(None, price, ts, reason)

    try:
        is_open = market_status(market, now).is_open
    except SMTError:
        is_open = False
    if not is_open:
        return skip(MARKET_CLOSED)
    if market == "FX" and now.astimezone(UTC).weekday() == 6:
        # FX opens Sunday 22:00 UTC, but its daily/weekly bars start Monday 00:00 UTC:
        # no Sunday bar, and the week's Monday would be the previous one
        return skip(MARKET_CLOSED)
    if price is None or ts is None:
        return skip(NO_QUOTE)
    start = period_start(market, tf, now)
    session_open = start
    if market in CALENDARS:
        session = session_on(market, local_date(market, now))
        if session is not None:
            session_open = session[0]
    if ts < max(start, session_open) or (now - ts).total_seconds() > stale_s:
        return skip(STALE_QUOTE)
    if closed and closed[-1].ts >= start:
        return skip(BAR_CLOSED)
    open_ = closed[-1].close if closed else price
    bar = Bar(start, open_, max(open_, price), min(open_, price), price, Decimal(0))
    return Forming(bar, price, ts, None)
