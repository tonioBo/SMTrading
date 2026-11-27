from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.pricing import (
    QuoteView,
    effective_bid_ask,
    fair_view,
    fill_price,
    market_price,
    notional_to_qty,
    round_to_tick,
)

TS = datetime(2026, 1, 1, tzinfo=UTC)


def q(bid=None, ask=None, last=None):
    return QuoteView(
        bid=None if bid is None else D(bid),
        ask=None if ask is None else D(ask),
        last=None if last is None else D(last),
        ts=TS,
    )


def test_market_buy_slippage_rounded_up():
    p = fill_price(
        OrderType.market,
        OrderSide.buy,
        q(bid="99", ask="100"),
        limit_price=None,
        slippage_bps=10,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D("100.10")


def test_market_sell_slippage_rounded_down():
    # 99.995 * 0.999 = 99.895005 -> 99.89
    p = fill_price(
        OrderType.market,
        OrderSide.sell,
        q(bid="99.995", ask="100"),
        limit_price=None,
        slippage_bps=10,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D("99.89")


def test_limit_buy_better_than_limit():
    p = fill_price(
        OrderType.limit,
        OrderSide.buy,
        q(bid="98", ask="99"),
        limit_price=D(100),
        slippage_bps=0,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D("99.00")


def test_limit_sell_better_than_limit():
    p = fill_price(
        OrderType.limit,
        OrderSide.sell,
        q(bid="101", ask="102"),
        limit_price=D(100),
        slippage_bps=0,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D("101.00")


def test_limit_buy_clamped_after_rounding():
    # 99.95 * 1.001 = 100.04995 -> 100.05 -> clamp 100.00
    p = fill_price(
        OrderType.limit,
        OrderSide.buy,
        q(bid="99", ask="99.95"),
        limit_price=D(100),
        slippage_bps=10,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D("100.00")


def test_limit_sell_clamped_after_rounding():
    # 100.05 * 0.999 = 99.949995 -> 99.94 -> clamp 100
    p = fill_price(
        OrderType.stop_limit,
        OrderSide.sell,
        q(bid="100.05", ask="101"),
        limit_price=D(100),
        slippage_bps=10,
        default_spread_bps=2,
        tick=D("0.01"),
    )
    assert p == D(100)


def test_limit_requires_price():
    with pytest.raises(SMTError) as e:
        fill_price(
            OrderType.limit,
            OrderSide.buy,
            q(bid="1", ask="2"),
            limit_price=None,
            slippage_bps=0,
            default_spread_bps=2,
            tick=D("0.01"),
        )
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_last_only_uses_default_spread():
    # last +- last * bps/2/10000: 50 * 5 / 2 / 10000 = 0.0125
    bid, ask = effective_bid_ask(q(last="50"), 5)
    assert bid == D("49.9875")
    assert ask == D("50.0125")


def test_partial_quote_filled_from_last():
    bid, ask = effective_bid_ask(q(bid="49", last="50"), 5)
    assert bid == D(49)
    assert ask == D("50.0125")


def test_partial_quote_without_last_uses_the_present_side_and_spread():
    # no zero-spread mirror: the missing side is the present one -+ bps/10000
    assert effective_bid_ask(q(ask="7"), 5) == (D("6.9965"), D(7))
    assert effective_bid_ask(q(bid="7"), 5) == (D(7), D("7.0035"))


@pytest.mark.parametrize(("kw", "expected"), [
    ({"bid": "51", "last": "50"}, (D(51), D("51.0255"))),  # last + half would be < bid
    ({"ask": "49", "last": "50"}, (D("48.9755"), D(49))),  # last - half would be > ask
    ({"bid": "50.0125", "last": "50"}, (D("50.0125"), D("50.03750625"))),  # would touch
])
def test_side_synthesised_from_last_never_crosses(kw, expected):
    bid, ask = effective_bid_ask(q(**kw), 5)
    assert (bid, ask) == expected and bid < ask


def test_no_price_raises():
    with pytest.raises(SMTError) as e:
        effective_bid_ask(q(), 5)
    assert e.value.code == ErrorCode.PROVIDER_UNAVAILABLE


def test_market_price_unrounded():
    assert market_price(
        OrderSide.buy, q(bid="99", ask="100"), slippage_bps=10, default_spread_bps=2
    ) == D("100.1")
    assert market_price(
        OrderSide.sell, q(bid="99.995", ask="100"), slippage_bps=10, default_spread_bps=2
    ) == D("99.895005")


@pytest.mark.parametrize(
    "price,tick,side,expected",
    [
        ("100.011", "0.01", OrderSide.buy, "100.02"),
        ("100.011", "0.01", OrderSide.sell, "100.01"),
        ("100.01", "0.01", OrderSide.buy, "100.01"),
        ("1.23456", "0.00001", OrderSide.buy, "1.23456"),
        ("1.234561", "0.00001", OrderSide.buy, "1.23457"),
        ("1.234569", "0.00001", OrderSide.sell, "1.23456"),
        ("1.2345", "0.001", OrderSide.buy, "1.235"),
        ("1.2349", "0.001", OrderSide.sell, "1.234"),
        ("10.3", "0.5", OrderSide.buy, "10.5"),
        ("10.7", "0.5", OrderSide.sell, "10.5"),
        ("5", "1", OrderSide.buy, "5"),
    ],
)
def test_round_to_tick(price, tick, side, expected):
    assert round_to_tick(D(price), D(tick), side) == D(expected)


def test_notional_to_qty():
    assert notional_to_qty(D(1000), D(190), D("0.000001")) == D("5.263157")


def test_notional_to_qty_zero_invalid():
    with pytest.raises(SMTError) as e:
        notional_to_qty(D(1000), D(1900), D(1))
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_fair_view_keeps_a_tight_book():
    v = fair_view(q(bid="99.90", ask="100.00", last="99.95"))
    assert (v.bid, v.ask, v.last) == (D("99.90"), D("100.00"), D("99.95"))


def test_fair_view_drops_a_wide_book_for_the_last_trade():
    # 27/11 JPM on IEX: bid 305.04 / ask 338.63, last 324.17
    v = fair_view(q(bid="305.04", ask="338.63", last="324.17"))
    assert (v.bid, v.ask, v.last) == (None, None, D("324.17"))
    assert effective_bid_ask(v, 2) == (D("324.17") - D("0.032417"), D("324.17") + D("0.032417"))


def test_fair_view_keeps_a_wide_book_without_a_last_trade():
    v = fair_view(q(bid="90", ask="110"))
    assert (v.bid, v.ask) == (D(90), D(110))


def test_fair_view_keeps_a_one_sided_quote():
    v = fair_view(q(bid="99", last="100"))
    assert (v.bid, v.ask, v.last) == (D(99), None, D(100))


def test_fair_view_clamps_a_last_above_the_book():
    # a stale last over the ask must never price a sell better than the book
    v = fair_view(q(bid="90", ask="110", last="120"))
    assert (v.bid, v.ask, v.last) == (None, None, D(110))


def test_fair_view_clamps_a_last_below_the_book():
    # a stale last under the bid must never fire a protective stop the book does not reach
    v = fair_view(q(bid="90", ask="110", last="80"))
    assert (v.bid, v.ask, v.last) == (None, None, D(90))
