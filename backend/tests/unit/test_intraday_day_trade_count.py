import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import OrderSide
from smtrading.intraday.day_trades import (
    FillRow,
    count_day_trades,
    day_trade_leverage,
    fresh_buying_power,
)
from smtrading.portfolios.models import Portfolio

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


def test_equal_timestamps_buys_sort_first():
    fills = [_f(B, SELL, 10, 0), _f(A, BUY, 10, 0)]  # sell listed first, same ts
    assert count_day_trades(fills) == {MON: 1}


def _acct(state, minutes_ago=9, kind="alpaca_paper"):
    at = None if minutes_ago is None else T0 - timedelta(minutes=minutes_ago)
    return BrokerLink(name="L", kind=kind, pdt_state=state, pdt_state_at=at)


def test_fresh_buying_power():
    bp = {"buying_power": "1000", "non_marginable_buying_power": "400"}
    assert fresh_buying_power(_acct(bp), T0) == D(1000)
    assert fresh_buying_power(_acct(bp), T0, crypto=True) == D(400)  # Alpaca's crypto rule
    assert fresh_buying_power(_acct(bp, minutes_ago=11), T0) is None
    assert fresh_buying_power(_acct(None), T0) is None
    assert fresh_buying_power(_acct(bp, minutes_ago=None), T0) is None
    assert fresh_buying_power(_acct({"daytrade_count": 1}), T0) is None  # an old PDT state
    assert fresh_buying_power(_acct({"buying_power": "abc"}), T0) is None
    assert fresh_buying_power(_acct({"buying_power": None}), T0) is None
    assert fresh_buying_power(_acct(bp, kind="t212_demo"), T0) is None
    assert fresh_buying_power(None, T0) is None


def test_day_trade_leverage():
    paper, live = _acct(None), _acct(None, kind="alpaca_live")
    assert day_trade_leverage(Portfolio(leverage=D(3)), paper) == D(2)
    assert day_trade_leverage(Portfolio(leverage=D("1.5")), paper) == D("1.5")
    assert day_trade_leverage(Portfolio(leverage=D(2)), None) == D(2)
    assert day_trade_leverage(Portfolio(leverage=D(2)), live) == D(1)  # real money: 1
