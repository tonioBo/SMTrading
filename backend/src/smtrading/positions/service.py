from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderSide
from smtrading.positions.models import Position

_Q18 = Decimal(1).scaleb(-18)


@dataclass(frozen=True)
class PositionState:
    qty: Decimal
    avg_price: Decimal
    realized_pnl: Decimal


def _q(value: Decimal) -> Decimal:
    return value.quantize(_Q18, rounding=ROUND_HALF_EVEN)


def apply_fill(state: PositionState, side: OrderSide, qty: Decimal, price: Decimal) -> PositionState:
    """Pure projection of one fill onto a position (signed qty, avg price, realized P&L)."""
    if qty <= 0 or price <= 0:
        raise SMTError(ErrorCode.INVALID_ORDER, "fill qty and price must be positive")
    with localcontext() as ctx:
        ctx.prec = 60
        delta = qty if side == OrderSide.buy else qty.copy_negate()
        old = state.qty
        new = old + delta
        avg = state.avg_price
        realized = state.realized_pnl
        if old == 0 or (old > 0) == (delta > 0):
            # opening or adding in the same direction: weighted average
            avg = (abs(old) * avg + qty * price) / (abs(old) + qty)
        else:
            closed = min(abs(old), qty)
            sign = 1 if old > 0 else -1
            realized = realized + (price - avg) * closed * sign
            if new == 0:
                avg = Decimal(0)
            elif (new > 0) != (old > 0):
                avg = price  # flipped: the remainder opens at the fill price
        return PositionState(qty=new, avg_price=_q(avg), realized_pnl=_q(realized))


async def upsert_position(
    session: AsyncSession,
    portfolio_id,
    symbol: str,
    side: OrderSide,
    qty: Decimal,
    price: Decimal,
) -> Position:
    """Apply a fill to the (portfolio, symbol) row, locking it. Caller holds the portfolio lock."""
    await session.execute(
        pg_insert(Position)
        .values(portfolio_id=portfolio_id, symbol=symbol, qty=0, avg_price=0, realized_pnl=0)
        .on_conflict_do_nothing()
    )
    row = (
        await session.execute(
            select(Position)
            .where(Position.portfolio_id == portfolio_id, Position.symbol == symbol)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    before = row.qty
    new = apply_fill(PositionState(row.qty, row.avg_price, row.realized_pnl), side, qty, price)
    row.qty, row.avg_price, row.realized_pnl = new.qty, new.avg_price, new.realized_pnl
    if new.qty == 0:
        row.opened_at = None
    elif before == 0 or row.opened_at is None:
        row.opened_at = datetime.now(UTC)
    await session.flush()
    return row


async def get_positions(session: AsyncSession, portfolio_id) -> list[Position]:
    result = await session.scalars(
        select(Position)
        .where(Position.portfolio_id == portfolio_id, Position.qty != 0)
        .order_by(Position.symbol)
    )
    return list(result)
