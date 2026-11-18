from decimal import Decimal

from smtrading.telegram.details import money


def test_euro_amounts_always_two_decimals():
    """Summary amounts in euro: a P/L under 1 € must not show the raw valuation decimals
    («+0,615333326 €» in the 18/11 daily summary)."""
    assert money(Decimal("0.615333326"), "EUR", sign=True) == "+0,62 €"
    assert money(Decimal("-0.004"), "EUR", sign=True) == "0,00 €"
    assert money(Decimal("1234.5"), "EUR") == "1.234,50 €"


def test_small_prices_keep_significant_decimals():
    assert money(Decimal("0.000123"), "USD") == "0,000123 USD"
