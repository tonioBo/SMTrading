from decimal import Decimal

import pytest

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import (
    canonical_symbol,
    get_instrument,
    validate_price,
    validate_quantity,
)


def _instr(symbol, lot, tick, **kw):
    return Instrument(
        symbol=symbol, name=symbol, asset_class=AssetClass.us_equity, market="X",
        currency="USD", lot_size=Decimal(lot), tick_size=Decimal(tick), **kw,
    )


AAPL = _instr("AAPL", "0.000001", "0.01")
ENI = _instr("ENI.MI", "1", "0.001")
BTC = _instr("BTC-USD", "0.00000001", "0.01")


def test_canonical_symbol():
    assert canonical_symbol(" aapl ") == "AAPL"


async def test_get_instrument_case_insensitive(db_session):
    db_session.add(_instr("AAPL", "0.000001", "0.01", active=True))
    await db_session.flush()
    assert (await get_instrument(db_session, " aapl ")).symbol == "AAPL"


async def test_unknown_symbol(db_session):
    with pytest.raises(SMTError) as e:
        await get_instrument(db_session, "NOPE")
    assert e.value.code == ErrorCode.SYMBOL_NOT_FOUND


async def test_inactive_symbol(db_session):
    db_session.add(_instr("OLD", "1", "0.01", active=False))
    await db_session.flush()
    with pytest.raises(SMTError) as e:
        await get_instrument(db_session, "old")
    assert e.value.code == ErrorCode.SYMBOL_NOT_FOUND


@pytest.mark.parametrize("qty,ok", [("1", True), ("0.5", False), ("0", False), ("-1", False)])
def test_validate_quantity(qty, ok):
    if ok:
        validate_quantity(ENI, Decimal(qty))
    else:
        with pytest.raises(SMTError) as e:
            validate_quantity(ENI, Decimal(qty))
        assert e.value.code == ErrorCode.INVALID_ORDER


def test_validate_crypto_quantity_8_decimals():
    validate_quantity(BTC, Decimal("0.12345678"))
    with pytest.raises(SMTError) as e:
        validate_quantity(BTC, Decimal("0.123456789"))
    assert e.value.code == ErrorCode.INVALID_ORDER


def test_validate_fractional_equity_quantity():
    validate_quantity(AAPL, Decimal("0.000001"))
    with pytest.raises(SMTError):
        validate_quantity(AAPL, Decimal("0.0000015"))


@pytest.mark.parametrize("price,ok", [("10.123", True), ("10.1234", False), ("0", False)])
def test_validate_price(price, ok):
    if ok:
        validate_price(ENI, Decimal(price))
    else:
        with pytest.raises(SMTError) as e:
            validate_price(ENI, Decimal(price))
        assert e.value.code == ErrorCode.INVALID_ORDER
