from decimal import ROUND_FLOOR, Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.instruments.models import Instrument

_PREC = 60


def canonical_symbol(raw: str) -> str:
    return raw.strip().upper()


async def get_instrument(session: AsyncSession, symbol: str) -> Instrument:
    canonical = canonical_symbol(symbol)
    instrument = await session.scalar(select(Instrument).where(Instrument.symbol == canonical))
    if instrument is None or not instrument.active:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "symbol not found", {"symbol": canonical})
    return instrument


def _validate_multiple(instr: Instrument, value: Decimal, step: Decimal, what: str) -> None:
    if value <= 0:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"{what} must be positive",
            {"symbol": instr.symbol, what: decimal_to_str(value)},
        )
    with localcontext() as ctx:
        ctx.prec = _PREC
        is_multiple = value % step == 0
    if not is_multiple:
        raise SMTError(
            ErrorCode.INVALID_ORDER, f"{what} is not a multiple of {decimal_to_str(step)}",
            {"symbol": instr.symbol, what: decimal_to_str(value), "step": decimal_to_str(step)},
        )


def floor_to_lot(qty: Decimal, lot: Decimal | None) -> Decimal:
    """`qty` rounded down to a multiple of `lot` (never up: an exit sized from what is
    held never sells more than it). A quantity already a multiple comes back as it is;
    below one lot: 0 (a remainder no order can carry). No lot (or not > 0): unchanged."""
    if lot is None or not lot > 0 or qty <= 0:
        return qty
    with localcontext() as ctx:
        ctx.prec = _PREC
        if qty % lot == 0:
            return qty
        return (qty / lot).to_integral_value(ROUND_FLOOR) * lot


async def lot_of(session: AsyncSession, symbol: str) -> Decimal | None:
    """The instrument's lot size (what `validate_quantity` checks); None when unknown."""
    return await session.scalar(select(Instrument.lot_size).where(Instrument.symbol == symbol))


def validate_quantity(instr: Instrument, qty: Decimal) -> None:
    _validate_multiple(instr, qty, instr.lot_size, "quantity")


def validate_price(instr: Instrument, price: Decimal) -> None:
    _validate_multiple(instr, price, instr.tick_size, "price")
