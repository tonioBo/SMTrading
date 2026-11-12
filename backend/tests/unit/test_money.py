from decimal import Decimal

import pytest

from smtrading.common.money import decimal_to_str, quantize_money


@pytest.mark.parametrize(
    "amt,ccy,exp",
    [("1.005", "EUR", "1.01"), ("1.004", "USD", "1.00"), ("150.5", "JPY", "151")],
)
def test_quantize_money(amt, ccy, exp):
    assert quantize_money(Decimal(amt), ccy) == Decimal(exp)


@pytest.mark.parametrize(
    "d,exp",
    [("1.500", "1.5"), ("0E-18", "0"), ("100", "100"), ("0.000000010", "0.00000001")],
)
def test_decimal_to_str(d, exp):
    assert decimal_to_str(Decimal(d)) == exp


def test_decimal_to_str_never_rounds_beyond_context_precision():
    s = "12345678901234567890.123456789012345678"
    assert decimal_to_str(Decimal(s)) == s
