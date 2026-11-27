"""The day quantity of a portfolio: the shares bought today by `day_trade` orders that are
still open (long only; short is out of scope). «Today» is the session of the instrument's
market; markets without a calendar (CRYPTO, FX) have no day."""
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.engine.models import OPEN_STATUSES, Fill, Order, OrderSide, OrderType
from smtrading.instruments.models import Instrument
from smtrading.intraday.calendar import session_window
from smtrading.positions.models import Position


async def day_quantities(
    session: AsyncSession, portfolio_id: uuid.UUID, now: datetime
) -> dict[str, Decimal]:
    """Per symbol: min(long position qty, max(0, bought today by day_trade orders - sold
    today)); only values > 0."""
    held = (await session.execute(
        select(Position.symbol, Position.qty, Instrument.market)
        .join(Instrument, Instrument.symbol == Position.symbol)
        .where(Position.portfolio_id == portfolio_id, Position.qty > 0)
    )).all()
    by_market: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for symbol, qty, market in held:
        by_market[market][symbol] = qty
    out: dict[str, Decimal] = {}
    for market, qtys in by_market.items():
        window = session_window(market, now)
        if window is None:
            continue
        rows = (await session.execute(
            select(Fill.symbol, Fill.side, Order.day_trade, Fill.qty)
            .join(Order, Order.id == Fill.order_id)
            .where(Fill.portfolio_id == portfolio_id, Fill.symbol.in_(qtys),
                   Fill.created_at >= window[0], Fill.created_at < window[1])
        )).all()
        net: dict[str, Decimal] = defaultdict(Decimal)
        for symbol, side, day_trade, qty in rows:
            if side == OrderSide.buy:
                if day_trade:
                    net[symbol] += qty
            else:
                net[symbol] -= qty
        for symbol, qty in qtys.items():
            q = min(qty, max(Decimal(0), net[symbol]))
            if q > 0:
                out[symbol] = q
    return out


async def selling_qty(session: AsyncSession, portfolio_id: uuid.UUID, symbol: str) -> Decimal:
    """What open reduce-only sells of the symbol are already selling (their quantity left):
    market orders (a close waiting for the broker, e.g. an automatic rule's exit with
    BrokerSync down) and stops or targets kept here that triggered and were handed to
    the broker. Resting exits do not count (the close cancels them)."""
    left = await session.scalar(
        select(func.coalesce(func.sum(Order.qty - Order.filled_qty), 0)).where(
            Order.portfolio_id == portfolio_id, Order.symbol == symbol,
            Order.side == OrderSide.sell, Order.reduce_only.is_(True),
            Order.status.in_(OPEN_STATUSES), Order.qty.is_not(None),
            (Order.type == OrderType.market)
            | (Order.synthetic.is_(True) & Order.broker_status.is_not(None))))
    return Decimal(left or 0)
