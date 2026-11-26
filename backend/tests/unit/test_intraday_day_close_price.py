"""The price of the end-of-session safety close (`intraday.day_close.close_limit_price`): at
market on a tight quote, a limit at the last trade minus the margin (floored to the tick)
on a wide one, nothing without a last trade."""
from datetime import UTC, datetime
from decimal import Decimal as D

from smtrading.intraday.day_close import close_limit_price
from smtrading.marketdata.types import Quote

TS = datetime(2026, 12, 1, 20, 50, tzinfo=UTC)


def _q(bid, ask, last):
    return Quote(symbol="AAPL", bid=None if bid is None else D(bid),
                 ask=None if ask is None else D(ask), last=None if last is None else D(last),
                 ts=TS, provider="fake")


def test_tight_quote_is_market():
    assert close_limit_price(_q("100", "100.5", "100.2"), D("0.5"), D("0.01")) == (False, None)


def test_wide_quote_uses_last_minus_margin():
    assert close_limit_price(_q("95", "105", "100"), D("0.5"), D("0.01")) == (True, D("99.50"))


def test_limit_floored_to_tick():
    wide, price = close_limit_price(_q("95", "105", "100.013"), D("0.5"), D("0.01"))
    assert wide is True and price == D("99.51")  # 99.512935 floored
    assert close_limit_price(_q("95", "105", "100.013"), D("0.5"), D("0.05")) == (
        True, D("99.50"))


def test_limit_without_tick_floors_to_cents():
    assert close_limit_price(_q("95", "105", "100.013"), D("0.5"), None) == (True, D("99.51"))


def test_wide_without_last_is_none():
    assert close_limit_price(_q("95", "105", None), D("0.5"), D("0.01")) == (True, None)
