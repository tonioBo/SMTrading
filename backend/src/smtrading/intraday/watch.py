"""Intraday signal triggers: at a bar close, the strategies on that timeframe whose entry
or exit rule went from false to true since the previous close (the state lives in Redis)."""
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.instruments.models import Instrument
from smtrading.intraday.calendar import day_close_at
from smtrading.intraday.models import INTRADAY_TIMEFRAMES
from smtrading.marketdata.types import Timeframe
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.signals.service import OK, Approved, _it, strategy_signals

STATE_KEY = "intraday:sig:{portfolio_id}:{ref}:{symbol}:{rule}"
STATE_TTL_S = 7 * 86400
_DIRECTION = {"below": "sotto", "above": "sopra", "cross_up": "incrocia al rialzo"}


@dataclass(frozen=True)
class Trigger:
    portfolio_id: uuid.UUID
    ref: str
    name: str
    symbol: str
    rule: Literal["entry", "exit"]
    timeframe: str
    bar_close: datetime
    value: Decimal | None
    threshold: Decimal | None
    price: Decimal | None
    rule_text: str
    late: bool


def intraday_selector(
    portfolios: dict[uuid.UUID, Portfolio], tf: Timeframe,
    exclude: frozenset[tuple[uuid.UUID, str]] = frozenset(),
) -> Callable[[Approved], Approved | None]:
    """Keeps an approved strategy only if it is on `tf` (an intraday timeframe), narrowed
    to the `portfolios` that watch `tf`, and to the symbols not excluded on all of them
    (the pairs of an automatic rule); nothing left: None."""
    def select_(a: Approved) -> Approved | None:
        if a.timeframe != tf.value or tf.value not in INTRADAY_TIMEFRAMES:
            return None
        kept = [p for p in a.portfolios
                if p.id in portfolios and tf.value in portfolios[p.id].intraday_timeframes]
        symbols = [s for s in a.symbols if any((p.id, s) not in exclude for p in kept)]
        if not kept or not symbols:
            return None
        return replace(a, portfolios=kept, symbols=symbols)
    return select_


async def _fire(redis: Any, key: str, now_value: bool) -> bool:
    """Stores the current value of a rule; True if it went from false to true."""
    prev = await redis.get(key)
    if isinstance(prev, bytes):
        prev = prev.decode()
    await redis.set(key, "1" if now_value else "0", ex=STATE_TTL_S)
    return now_value and prev == "0"


async def evaluate_close(
    session: AsyncSession, router: Any, redis: Any, tf: Timeframe, market: str,
    bar_close: datetime, now: datetime, *,
    exclude: frozenset[tuple[uuid.UUID, str]] = frozenset(),
) -> list[Trigger]:
    """The triggers of the `tf` bar that closed at `bar_close` on `market`."""
    portfolios = {p.id: p for p in await session.scalars(
        select(Portfolio).where(Portfolio.intraday_enabled.is_(True),
                                Portfolio.status != PortfolioStatus.archived))}
    if not portfolios:
        return []
    sigs = await strategy_signals(
        session, router, now, select=intraday_selector(portfolios, tf, exclude))
    symbols = {r.symbol for s in sigs for r in s.symbols}
    markets = {i.symbol: i.market for i in await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(symbols)))} if symbols else {}
    out: list[Trigger] = []
    for s in sigs:
        p = portfolios.get(s.portfolio_id) if s.portfolio_id else None
        if p is None or s.status != OK:
            continue
        cutoff = day_close_at(market, now, p.day_close_minutes)
        for r in s.symbols:
            if markets.get(r.symbol) != market or (p.id, r.symbol) in exclude:
                continue
            base = {"portfolio_id": p.id, "ref": s.ref, "name": s.name, "symbol": r.symbol,
                    "timeframe": tf.value, "bar_close": bar_close, "price": r.last}
            entry = bool(r.triggered_closed) and r.position is None and not r.open_entry
            key = STATE_KEY.format(portfolio_id=p.id, ref=s.ref, symbol=r.symbol, rule="entry")
            if await _fire(redis, key, entry):
                out.append(Trigger(
                    **base, rule="entry", value=r.value_closed, threshold=r.threshold,
                    rule_text=f"{r.indicator} {_DIRECTION.get(r.direction, r.direction)} "
                              f"{_it(r.threshold) if r.threshold is not None else ''}".strip(),
                    late=cutoff is not None and bar_close >= cutoff))
            ex = r.exit if r.position is not None else None
            key = STATE_KEY.format(portfolio_id=p.id, ref=s.ref, symbol=r.symbol, rule="exit")
            if await _fire(redis, key, bool(ex and ex["triggered"])):
                out.append(Trigger(
                    **base, rule="exit", value=ex["value"], threshold=ex["threshold"],
                    rule_text=ex["rule"], late=False))
    return out
