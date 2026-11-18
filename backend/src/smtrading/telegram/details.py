"""Richer Telegram texts for people (never for the agents): the details of a fill
notification (strategy, stop and target of its bracket/OCO, risk at stop in EUR) and the
«Posizioni aperte» section of the daily and weekly summaries.

Every figure is best effort: what is unknown (no strategy, no protection, an FX rate or a
quote that cannot be read) is left out, never guessed, and never blocks the message."""

import logging
import uuid
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Activity
from smtrading.common.errors import SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import FILLABLE_STATUSES, OPEN_STATUSES, Order, OrderSide
from smtrading.instruments.models import Instrument
from smtrading.portfolios.models import Portfolio, live_portfolio_ids
from smtrading.positions.open_view import STOP_TYPES, is_active_exit, stop_level
from smtrading.strategies.attribution import strategy_of_order
from smtrading.valuation.service import value_portfolio

log = logging.getLogger(__name__)

EUR = "EUR"
_Q2 = Decimal("0.01")
FILL_TYPES = ("order.filled", "live.order.filled", "live.order.partial")


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _grouped(q: Decimal) -> str:
    return f"{q:,f}".replace(",", "_").replace(".", ",").replace("_", ".")


def money(value: Decimal, ccy: str | None, *, sign: bool = False) -> str:
    """Italian money: `1.234,50 €`, `242,20 USD`, `+0,35 €` with `sign`; euro amounts are
    always rounded to cents; other prices below 1 keep their significant decimals
    (`0,000123 USD`)."""
    if abs(value) >= 1 or value == 0 or ccy == EUR:  # euro amounts: always cents
        value = value.quantize(_Q2, rounding=ROUND_HALF_EVEN) + 0  # no «-0,00»
        text = _grouped(value)
    else:
        text = decimal_to_str(value).replace(".", ",")
    if sign and value > 0:
        text = "+" + text
    unit = "€" if ccy == EUR else ccy
    return f"{text} {unit}" if unit else text


def qty_text(value: Decimal) -> str:
    """`0,51`, `2`, `-1,5`: Italian, no trailing zeros."""
    return decimal_to_str(value).replace(".", ",")


def pct_text(value: Decimal) -> str:
    q = value.quantize(_Q2, rounding=ROUND_HALF_EVEN)
    return ("+" if q > 0 else "") + _grouped(q) + "%"


async def _to_eur(qs, amount: Decimal, ccy: str | None) -> Decimal | None:
    if ccy == EUR:
        return amount
    if qs is None or not ccy:
        return None
    try:
        return amount * await qs.get_fx_rate(ccy, EUR)
    except Exception as e:  # noqa: BLE001 - a missing rate only drops the figure
        log.info("telegram: no %s->EUR rate for the fill details (%s)", ccy, e)
        return None


async def _protection(session: AsyncSession, order: Order) -> tuple[Decimal | None,
                                                                     Decimal | None]:
    """(stop, target) of the open bracket legs of `order` (an exit leg has none)."""
    legs = list(await session.scalars(
        select(Order).where(Order.parent_id == order.id, Order.status.in_(OPEN_STATUSES))
        .order_by(Order.created_at, Order.id)
    ))
    stop = next((leg.stop_price for leg in legs
                 if leg.stop_price is not None and leg.role != "take_profit"), None)
    target = next((leg.limit_price for leg in legs
                   if leg.role == "take_profit" and leg.limit_price is not None), None)
    return stop, target


async def fill_lines(session: AsyncSession, a: Activity, *, qs=None) -> list[str]:
    """The extra lines of a fill notification: «Strategia: STR-103», «Stop 242,20 USD ·
    obiettivo 284,00 USD», «Rischio allo stop: 7,75 €». [] for other rows or an order
    that cannot be found."""
    if a.type not in FILL_TYPES:
        return []
    data = a.data if isinstance(a.data, dict) else {}
    try:
        order_id = uuid.UUID(str(data.get("order_id")))
    except ValueError:
        return []
    order = await session.get(Order, order_id)
    if order is None:
        return []
    lines: list[str] = []
    if strategy := await strategy_of_order(session, order):
        lines.append(f"Strategia: {strategy}")
    stop, target = await _protection(session, order)
    ccy = data.get("currency") or await session.scalar(
        select(Instrument.currency).where(Instrument.symbol == order.symbol))
    parts = []
    if stop is not None:
        parts.append(f"Stop {money(stop, ccy)}")
    if target is not None:
        parts.append(f"obiettivo {money(target, ccy)}")
    if parts:
        lines.append(" · ".join(parts))
    price = order.avg_fill_price or _dec(data.get("price"))
    qty = order.filled_qty or _dec(data.get("filled_qty")) or _dec(data.get("qty"))
    if stop is not None and price is not None and qty:
        per_unit = price - stop if order.side == OrderSide.buy else stop - price
        if per_unit > 0:
            risk = per_unit * abs(qty)
            eur = await _to_eur(qs, risk, ccy)
            shown = money(eur, EUR) if eur is not None else money(risk, ccy)
            lines.append(f"Rischio allo stop: {shown}")
    return lines


# -- «Posizioni aperte» -------------------------------------------------------------------


async def _stops(session: AsyncSession, portfolio_id: uuid.UUID) -> dict[tuple[str, str], Decimal]:
    """(symbol, side) -> the tightest active stop level (highest sell stop, lowest buy
    stop), by the protection alarm's rules: a resting order not being canceled (a held
    bracket child is not active yet), stop / stop_limit / trailing_stop (its level)."""
    orders = await session.scalars(
        select(Order).where(Order.portfolio_id == portfolio_id,
                            Order.status.in_(FILLABLE_STATUSES),
                            Order.type.in_(STOP_TYPES)))
    out: dict[tuple[str, str], Decimal] = {}
    for o in orders:
        price = stop_level(o)
        if price is None or not is_active_exit(o):
            continue
        key = (o.symbol, str(o.side))
        best = out.get(key)
        if best is None or (price > best if o.side == OrderSide.sell else price < best):
            out[key] = price
    return out


async def open_positions_lines(session: AsyncSession, qs, *, redis=None) -> list[str]:
    """One line per open position of the portfolios that are not archived:
    `AMZN 0,51 · 131,40 € · -0,35 € (-0,27%) · stop 242,20 USD — Aggressivo`
    (`stop nessuno` when unprotected); a portfolio whose valuation fails gets one
    «valori non disponibili» line. [] when there is no position."""
    portfolios = (await session.execute(
        select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(live_portfolio_ids()))
        .order_by(Portfolio.name)
    )).all()
    lines: list[str] = []
    for pid, name in portfolios:
        where = " ".join(name.split())
        try:
            async with session.begin_nested():  # a failure never spoils the summary
                valuation = await value_portfolio(session, qs, pid, redis=redis)
                stops = await _stops(session, pid) if valuation.positions else {}
        except Exception as e:  # noqa: BLE001 - one line instead of the positions
            level = logging.INFO if isinstance(e, SMTError) else logging.WARNING
            log.log(level, "telegram summary: portfolio %s not valued (%s)", pid,
                    getattr(e, "code", type(e).__name__))
            lines.append(f"{where}: valori non disponibili")
            continue
        for p in valuation.positions:
            exit_side = "sell" if p.qty > 0 else "buy"
            stop = stops.get((p.symbol, exit_side))
            cost = abs(p.qty) * p.avg_price
            pnl = money(p.unrealized_pnl_eur, EUR, sign=True)
            if cost:
                pnl += f" ({pct_text(p.unrealized_pnl / cost * 100)})"
            stop_text = money(stop, p.currency) if stop is not None else "nessuno"
            lines.append(f"{p.symbol} {qty_text(p.qty)} · {money(p.market_value_eur, EUR)}"
                         f" · {pnl} · stop {stop_text} — {where}")
    return lines
