from decimal import Decimal as D

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide
from smtrading.positions.service import PositionState, apply_fill

ZERO = PositionState(D(0), D(0), D(0))


def test_sequence_from_brief():
    s = apply_fill(ZERO, OrderSide.buy, D(10), D(100))
    assert (s.qty, s.avg_price, s.realized_pnl) == (10, 100, 0)
    s = apply_fill(s, OrderSide.buy, D(10), D(110))
    assert (s.qty, s.avg_price, s.realized_pnl) == (20, 105, 0)
    s = apply_fill(s, OrderSide.sell, D(5), D(120))
    assert (s.qty, s.avg_price, s.realized_pnl) == (15, 105, 75)
    s = apply_fill(s, OrderSide.sell, D(20), D(100))
    assert (s.qty, s.avg_price, s.realized_pnl) == (-5, 100, 0)
    s = apply_fill(s, OrderSide.buy, D(5), D(90))
    assert (s.qty, s.avg_price, s.realized_pnl) == (0, 0, 50)


def test_short_open_and_add_weighted_average():
    s = apply_fill(ZERO, OrderSide.sell, D(4), D(50))
    s = apply_fill(s, OrderSide.sell, D(4), D(60))
    assert (s.qty, s.avg_price) == (-8, 55)


def test_cover_short_partially_realizes_profit():
    s = PositionState(D(-10), D(100), D(0))
    s = apply_fill(s, OrderSide.buy, D(4), D(90))
    assert (s.qty, s.avg_price, s.realized_pnl) == (-6, 100, 40)


def test_flip_long_to_short():
    s = PositionState(D(5), D(100), D(0))
    s = apply_fill(s, OrderSide.sell, D(8), D(110))
    assert (s.qty, s.avg_price, s.realized_pnl) == (-3, 110, 50)


def test_exact_close_resets_avg():
    s = apply_fill(PositionState(D(5), D(100), D(7)), OrderSide.sell, D(5), D(90))
    assert (s.qty, s.avg_price, s.realized_pnl) == (0, 0, -43)


def test_results_quantized_to_18_decimals():
    s = apply_fill(ZERO, OrderSide.buy, D(1), D(1))
    s = apply_fill(s, OrderSide.buy, D(2), D(1) + D(1) / D(3) * 0 + D("0.5"))
    s = apply_fill(s, OrderSide.buy, D(1), D("1.1"))
    assert s.avg_price.as_tuple().exponent >= -18
    assert s.realized_pnl.as_tuple().exponent >= -18
    third = apply_fill(PositionState(D(3), D(1), D(0)), OrderSide.buy, D(4), D(2))
    assert third.avg_price == D("1.571428571428571429")


@pytest.mark.parametrize("qty,price", [(D(0), D(1)), (D(-1), D(1)), (D(1), D(0)), (D(1), D(-5))])
def test_invalid_inputs(qty, price):
    with pytest.raises(SMTError) as e:
        apply_fill(ZERO, OrderSide.buy, qty, price)
    assert e.value.code == ErrorCode.INVALID_ORDER
