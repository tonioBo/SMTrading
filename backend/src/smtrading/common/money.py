from decimal import ROUND_HALF_UP, Decimal

_ZERO_DECIMAL_CURRENCIES = {"JPY"}


def quantize_money(amount: Decimal, currency: str, rounding: str = ROUND_HALF_UP) -> Decimal:
    exp = Decimal(1) if currency.upper() in _ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    return amount.quantize(exp, rounding=rounding)


def decimal_to_str(d: Decimal) -> str:
    if d == 0:
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
