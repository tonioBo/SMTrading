from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from smtrading.common.errors import SMTError
from smtrading.marketdata.types import Quote
from smtrading.valuation.service import mark_price

TS = datetime(2026, 11, 19, 21, 2, tzinfo=UTC)


def q(bid=None, ask=None, last=None):
    return Quote(symbol="NVDA", bid=None if bid is None else D(bid), ask=None if ask is None else D(ask),
                 last=None if last is None else D(last), ts=TS, provider="alpaca")


def test_tight_spread_uses_mid():
    assert mark_price(q("100.00", "100.10", "99")) == D("100.05")


def test_wide_after_hours_spread_uses_last():
    # the real case of 19/11: mid 161.9 would value the position 11% above any trade
    assert mark_price(q("145.6", "178.29", "145.91")) == D("145.91")


def test_wide_spread_without_last_keeps_mid():
    assert mark_price(q("10", "12")) == D(11)


def test_only_last():
    assert mark_price(q(last="5")) == D(5)


def test_no_price():
    with pytest.raises(SMTError):
        mark_price(q())
