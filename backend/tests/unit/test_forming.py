"""The bar still forming («se chiudesse ora»): built from the current quote only while
the market is open, the quote fresh and from today's session, for 1d and 1wk."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from smtrading.engine.quotesource import FakeMarketData
from smtrading.instruments.market_hours import Closure, local_date, set_closures
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.marketdata.forming import forming_bar, period_start
from smtrading.marketdata.types import Bar, Timeframe

D = Decimal
SPY = Instrument(symbol="SPY", name="SPY", asset_class=AssetClass.us_equity, market="NYSE",
                 currency="USD")
ENEL = Instrument(symbol="ENEL.MI", name="Enel", asset_class=AssetClass.eu_equity,
                  market="MTA", currency="EUR")
BTC = Instrument(symbol="BTC-USD", name="Bitcoin", asset_class=AssetClass.crypto,
                 market="CRYPTO", currency="USD")
TUE_US_OPEN = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)  # NYSE open, MTA closed


@pytest.fixture(autouse=True)
def _no_closures():
    set_closures([])
    yield
    set_closures([])


def _daily(last_day: date, n: int = 5) -> list[Bar]:
    return [Bar(datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC)
                - timedelta(days=n - 1 - i), D(100), D(102), D(98), D(100 + i), D(1))
            for i in range(n)]


def _qs(symbol: str, last, ts: datetime, bid=None, ask=None) -> FakeMarketData:
    qs = FakeMarketData()
    qs.set_quote(symbol, bid, ask, None if last is None else D(last), ts)
    return qs


def test_local_date_and_period_start():
    late = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)  # Wednesday 00:30 in Milan
    assert local_date("MTA", late) == date(2026, 11, 18)
    assert local_date("NYSE", late) == date(2026, 11, 17)
    assert local_date("CRYPTO", late) == date(2026, 11, 17)
    assert period_start("NYSE", Timeframe.d1, late) == datetime(2026, 11, 17, tzinfo=UTC)
    assert period_start("MTA", Timeframe.w1, late) == datetime(2026, 11, 16, tzinfo=UTC)


async def test_open_market_appends_bar_from_quote():
    closed = _daily(date(2026, 11, 16))
    qs = _qs("SPY", "110", TUE_US_OPEN - timedelta(seconds=30))
    f = await forming_bar(qs, SPY, Timeframe.d1, closed, 120, now=TUE_US_OPEN)
    assert f.skipped is None and f.price == D(110)
    assert f.quote_ts == TUE_US_OPEN - timedelta(seconds=30)
    assert f.bar == Bar(datetime(2026, 11, 17, tzinfo=UTC), D(104), D(110), D(104), D(110),
                        D(0))  # open = previous close


async def test_mid_when_no_last_and_low_below_open():
    closed = _daily(date(2026, 11, 16))
    qs = _qs("SPY", None, TUE_US_OPEN, bid=D("99"), ask=D("101"))
    f = await forming_bar(qs, SPY, Timeframe.d1, closed, 120, now=TUE_US_OPEN)
    assert f.price == D(100) and f.bar.low == D(100) and f.bar.high == D(104)


async def test_closed_market_weekend_and_holiday_add_nothing():
    closed = _daily(date(2026, 11, 16))
    qs = _qs("ENEL.MI", "6", TUE_US_OPEN)  # MTA closed at 18:00 UTC
    f = await forming_bar(qs, ENEL, Timeframe.d1, closed, 1200, now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "market closed" and f.price == D(6)

    saturday = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    f = await forming_bar(_qs("SPY", "1", saturday), SPY, Timeframe.d1, closed, 120,
                          now=saturday)
    assert f.bar is None and f.skipped == "market closed"

    thanksgiving = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
    f = await forming_bar(_qs("SPY", "1", thanksgiving), SPY, Timeframe.d1, closed, 120,
                          now=thanksgiving)
    assert f.bar is None and f.skipped == "market closed"

    set_closures([Closure("NYSE", date(2026, 11, 17), "closed", None)])  # manual closure
    f = await forming_bar(_qs("SPY", "1", TUE_US_OPEN), SPY, Timeframe.d1, closed, 120,
                          now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "market closed"


async def test_stale_or_previous_session_quote_adds_nothing():
    closed = _daily(date(2026, 11, 16))
    old = TUE_US_OPEN - timedelta(seconds=121)
    f = await forming_bar(_qs("SPY", "1", old), SPY, Timeframe.d1, closed, 120,
                          now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "stale quote" and f.quote_ts == old
    # EU quotes 15 minutes late: fresh enough for eu_equity (1200 s), as_of says how late
    eu_now = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    late = eu_now - timedelta(minutes=15)
    f = await forming_bar(_qs("ENEL.MI", "6", late), ENEL, Timeframe.d1, closed, 1200,
                          now=eu_now)
    assert f.bar is not None and f.quote_ts == late
    # a quote from before today's open is yesterday's close, not today's bar
    just_open = datetime(2026, 11, 17, 8, 5, tzinfo=UTC)  # MTA opens 08:00 UTC
    f = await forming_bar(_qs("ENEL.MI", "6", just_open - timedelta(minutes=10)), ENEL,
                          Timeframe.d1, closed, 1200, now=just_open)
    assert f.bar is None and f.skipped == "stale quote"


async def test_no_quote_other_timeframes_and_bar_already_closed():
    closed = _daily(date(2026, 11, 16))
    f = await forming_bar(FakeMarketData(), SPY, Timeframe.d1, closed, 120, now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "no quote" and f.price is None
    f = await forming_bar(_qs("SPY", "1", TUE_US_OPEN), SPY, Timeframe.h1, closed, 120,
                          now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "timeframe not supported"
    today = _daily(date(2026, 11, 17))
    f = await forming_bar(_qs("SPY", "1", TUE_US_OPEN), SPY, Timeframe.d1, today, 120,
                          now=TUE_US_OPEN)
    assert f.bar is None and f.skipped == "bar already closed"


async def test_weekly_and_crypto():
    sunday = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    weeks = [Bar(datetime(2026, 11, 9, tzinfo=UTC), D(1), D(2), D(1), D(50), D(1)),
             Bar(datetime(2026, 11, 16, tzinfo=UTC), D(1), D(2), D(1), D(60), D(1))]
    f = await forming_bar(_qs("BTC-USD", "70", sunday), BTC, Timeframe.w1, weeks, 60,
                          now=sunday)
    assert f.skipped == "bar already closed"  # this week's bar is already there
    f = await forming_bar(_qs("BTC-USD", "70", sunday), BTC, Timeframe.w1, weeks[:1], 60,
                          now=sunday)
    assert f.bar is not None and f.bar.ts == datetime(2026, 11, 16, tzinfo=UTC)
    assert (f.bar.open, f.bar.close) == (D(50), D(70))
    f = await forming_bar(_qs("BTC-USD", "70", sunday), BTC, Timeframe.d1, [], 60,
                          now=sunday)
    assert f.bar is not None and f.bar.open == D(70)  # no previous close: the price
