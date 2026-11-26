import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from smtrading.engine.models import OrderSide
from smtrading.intraday.pdt import FillRow, count_day_trades, pdt_applies

MON = date(2026, 11, 23)
BUY, SELL = OrderSide.buy, OrderSide.sell
T0 = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)


def _f(order, side, qty, minute=0, day=MON, symbol="AAPL"):
    return FillRow(order_id=order, symbol=symbol, side=side, qty=D(qty), day=day,
                   ts=T0 + timedelta(minutes=minute))


A, B, C, D4 = (uuid.uuid4() for _ in range(4))


def test_buy_buy_sell_is_one():
    assert count_day_trades([_f(A, BUY, 10, 0), _f(B, BUY, 10, 1), _f(C, SELL, 20, 2)]) == {MON: 1}


def test_buy_sell_buy_sell_is_two():
    fills = [_f(A, BUY, 10, 0), _f(B, SELL, 10, 1), _f(C, BUY, 10, 2), _f(D4, SELL, 10, 3)]
    assert count_day_trades(fills) == {MON: 2}


def test_buy_sell_sell_is_two():
    fills = [_f(A, BUY, 10, 0), _f(B, SELL, 5, 1), _f(C, SELL, 5, 2)]
    assert count_day_trades(fills) == {MON: 2}


def test_two_fills_of_one_sell_order_count_once():
    fills = [_f(A, BUY, 10, 0), _f(B, SELL, 4, 1), _f(B, SELL, 6, 2)]
    assert count_day_trades(fills) == {MON: 1}


def test_overnight_shares_sold_today_is_not_a_day_trade():
    assert count_day_trades([_f(A, SELL, 10, 0)]) == {}


def test_days_are_separate():
    tue = date(2026, 11, 24)
    fills = [_f(A, BUY, 10, 0), _f(B, SELL, 10, 0, day=tue)]
    assert count_day_trades(fills) == {}


def test_symbols_are_separate():
    fills = [_f(A, BUY, 10, 0), _f(B, SELL, 10, 1, symbol="MSFT")]
    assert count_day_trades(fills) == {}


@pytest.mark.parametrize("kind, state, expected", [
    ("t212_demo", None, False),
    ("alpaca_paper", None, True),
    ("alpaca_paper", {"multiplier": "2", "equity": "24999.99"}, True),
    ("alpaca_paper", {"multiplier": "1", "equity": "1000"}, False),
    ("alpaca_paper", {"multiplier": "4", "equity": "30000", "last_equity": "24000"}, True),
    ("alpaca_paper", {"multiplier": "4", "equity": "30000", "last_equity": "30000"}, False),
    ("alpaca_live", {"multiplier": "2"}, True),
])
def test_pdt_applies(kind, state, expected):
    assert pdt_applies(kind, state) is expected


def test_equal_timestamps_buys_sort_first():
    fills = [_f(B, SELL, 10, 0), _f(A, BUY, 10, 0)]  # sell listed first, same ts
    assert count_day_trades(fills) == {MON: 1}
