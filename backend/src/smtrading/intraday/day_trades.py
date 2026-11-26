"""Conteggio delle operazioni di giornata dai nostri riempimenti, per il tetto facoltativo
del portafoglio e per le misure; la regola PDT non esiste più dal 4/6/2026.

The count comes from our own fills: a sell that closes quantity bought in the same session
counts once per sell order. `max_day_trades_5d` caps only its own portfolio; a broker link
has no shared budget. The broker's account state (`BrokerLink.pdt_state`, read by
`intraday.account_reader`) gives the buying power a day-trade entry may not exceed, and
`day_trade_leverage` the leverage a day-trade entry may use."""
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import FILLABLE_STATUSES, Fill, Order, OrderSide
from smtrading.instruments.market_hours import local_date, session_on
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.intraday.calendar import last_sessions, session_after
from smtrading.intraday.day_positions import day_quantities
from smtrading.portfolios.models import Portfolio

# the links whose broker account is read (account_reader)
ACCOUNT_KINDS = frozenset({"alpaca_paper", "alpaca_live"})
ACCOUNT_READ_INTERVAL_S = 300
ACCOUNT_STATE_MAX_AGE = timedelta(seconds=2 * ACCOUNT_READ_INTERVAL_S)  # two read rounds
DAY_TRADE_MARKET = "NYSE"
DAY_TRADE_SESSIONS = 5
# day-trade entries on a test link; on a real-money link always 1 (spec, point 6)
DAY_TRADE_MAX_LEVERAGE = Decimal(2)


@dataclass(frozen=True)
class FillRow:
    order_id: uuid.UUID
    symbol: str
    side: OrderSide
    qty: Decimal
    day: date
    ts: datetime


@dataclass(frozen=True)
class DayTradeUsage:
    limit: int | None
    used: int
    pending: int
    remaining: int | None
    frees_on: date | None


def count_day_trades(fills: Iterable[FillRow]) -> dict[date, int]:
    """Day trades per session day. Cautious: a sell order that closes quantity bought in
    the same session counts once (buy-buy-sell = 1, buy-sell-buy-sell = 2,
    buy-sell-sell = 2); a sell of overnight shares alone counts 0."""
    out: dict[date, int] = defaultdict(int)
    # equal timestamps (one transaction): buys first, never undercount
    ordered = sorted(fills, key=lambda f: (f.day, f.symbol, f.ts, f.side != OrderSide.buy))
    for (day, _symbol), rows in groupby(ordered, key=lambda f: (f.day, f.symbol)):
        bought = Decimal(0)
        counted: set[uuid.UUID] = set()
        for f in rows:
            if f.side == OrderSide.buy:
                bought += f.qty
                continue
            matched = min(f.qty, bought)
            if matched > 0:
                bought -= matched
                if f.order_id not in counted:
                    counted.add(f.order_id)
                    out[day] += 1
    return dict(out)


def _window(now: datetime) -> list[date]:
    return last_sessions(DAY_TRADE_MARKET, now, DAY_TRADE_SESSIONS)


async def _day_trades(
    session: AsyncSession, now: datetime, *, portfolio_id: uuid.UUID
) -> dict[date, int]:
    """Day trades per session of the window, from the fills of a portfolio on us_equity
    instruments."""
    days = _window(now)
    if not days:
        return {}
    start = session_on(DAY_TRADE_MARKET, days[0])[0]
    q = (select(Fill.order_id, Fill.symbol, Fill.side, Fill.qty, Fill.created_at)
         .join(Instrument, Instrument.symbol == Fill.symbol)
         .where(Instrument.asset_class == AssetClass.us_equity, Fill.created_at >= start,
                Fill.portfolio_id == portfolio_id))
    rows = [FillRow(o, s, side, qty, local_date(DAY_TRADE_MARKET, ts), ts)
            for o, s, side, qty, ts in (await session.execute(q)).all()]
    return {d: n for d, n in count_day_trades(rows).items() if d in days}


async def _pending_pairs(
    session: AsyncSession, now: datetime, portfolio_id: uuid.UUID
) -> set[tuple[uuid.UUID, str]]:
    """(portfolio, symbol) pairs that will cost an operation: an open day position, or a
    resting `day_trade` buy (not reduce-only) without one."""
    pairs = {(portfolio_id, s) for s in await day_quantities(session, portfolio_id, now)}
    resting = (await session.execute(
        select(Order.portfolio_id, Order.symbol)
        .join(Instrument, Instrument.symbol == Order.symbol)
        .where(Order.portfolio_id == portfolio_id, Order.side == OrderSide.buy,
               Order.day_trade.is_(True), Order.reduce_only.is_(False),
               Order.status.in_(FILLABLE_STATUSES),
               Instrument.asset_class == AssetClass.us_equity)
    )).all()
    return pairs | {(pid, s) for pid, s in resting}


async def _us_pairs(session: AsyncSession, pairs: set[tuple[uuid.UUID, str]]):
    if not pairs:
        return set()
    us = set((await session.execute(
        select(Instrument.symbol).where(
            Instrument.symbol.in_({s for _, s in pairs}),
            Instrument.asset_class == AssetClass.us_equity)
    )).scalars())
    return {(p, s) for p, s in pairs if s in us}


def _frees_on(per_day: dict[date, int]) -> date | None:
    busy = [d for d, n in per_day.items() if n > 0]
    return session_after(DAY_TRADE_MARKET, min(busy), DAY_TRADE_SESSIONS) if busy else None


async def own_cap_usage(
    session: AsyncSession, portfolio: Portfolio, now: datetime, symbol: str | None = None
) -> DayTradeUsage:
    """The portfolio's own day trades in the window and what its cap `max_day_trades_5d`
    leaves (`limit`/`remaining` None without a cap). A `symbol` that already has a day
    position or a resting day-trade buy adds no future operation; None (the portal)
    releases nothing."""
    per_day = await _day_trades(session, now, portfolio_id=portfolio.id)
    used = sum(per_day.values())
    pairs = await _us_pairs(session, await _pending_pairs(session, now, portfolio.id))
    pending = len(pairs) - (1 if symbol is not None and (portfolio.id, symbol) in pairs else 0)
    limit = portfolio.max_day_trades_5d
    return DayTradeUsage(
        limit=limit, used=used, pending=pending,
        remaining=None if limit is None else max(0, limit - used - pending),
        frees_on=_frees_on(per_day),
    )


def day_trade_leverage(portfolio: Portfolio, link: BrokerLink | None) -> Decimal:
    """The leverage a day-trade entry may use: 1 on a real-money link whatever the
    portfolio's setting (until decided otherwise), else the portfolio's leverage up to
    DAY_TRADE_MAX_LEVERAGE."""
    if link is not None and link.kind in LIVE_KINDS:
        return Decimal(1)
    return min(portfolio.leverage, DAY_TRADE_MAX_LEVERAGE)


def fresh_buying_power(
    link: BrokerLink | None, now: datetime, *, crypto: bool = False
) -> Decimal | None:
    """The broker's buying power from the account state read in the last
    ACCOUNT_STATE_MAX_AGE (`non_marginable_buying_power` for crypto, Alpaca's rule for
    it); None without a link of ACCOUNT_KINDS, a state, a fresh read or a number (an old
    PDT state included)."""
    if link is None or link.kind not in ACCOUNT_KINDS:
        return None
    state, at = link.pdt_state, link.pdt_state_at
    if not isinstance(state, dict) or at is None or now - at > ACCOUNT_STATE_MAX_AGE:
        return None
    raw = state.get("non_marginable_buying_power" if crypto else "buying_power")
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None
