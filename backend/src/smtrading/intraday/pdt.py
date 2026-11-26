"""Pattern day trader (PDT) count and budgets. The count comes from our own fills (the
broker's `daytrade_count` is only compared): a sell that closes quantity bought in the same
session counts once per sell order. The budget of a broker link is shared by all its
portfolios and both intraday modes; `max_day_trades_5d` counts only its own portfolio."""
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.brokers.models import BrokerLink
from smtrading.engine.models import FILLABLE_STATUSES, Fill, Order, OrderSide
from smtrading.instruments.market_hours import local_date, session_on
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.intraday.calendar import last_sessions, session_after
from smtrading.intraday.day_positions import day_quantities
from smtrading.portfolios.models import Portfolio

PDT_KINDS = frozenset({"alpaca_paper", "alpaca_live"})
PDT_MARKET = "NYSE"
PDT_MAX = 3
PDT_SESSIONS = 5
PDT_EQUITY_MIN = Decimal(25000)


@dataclass(frozen=True)
class FillRow:
    order_id: uuid.UUID
    symbol: str
    side: OrderSide
    qty: Decimal
    day: date
    ts: datetime


@dataclass(frozen=True)
class PdtBudget:
    applies: bool
    limit: int | None
    used: int
    pending: int
    remaining: int | None
    frees_on: date | None
    broker_count: int | None
    mismatch: bool


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


def _dec(v) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except ArithmeticError:
        return None


def pdt_applies(kind: str, state: dict | None) -> bool:
    """False for a broker without the PDT rule; True when the state was never read (cautious)
    or the account is on margin (multiplier > 1) with equity under 25,000 $ (the lower of
    equity and last equity; missing values ignored, all missing -> True; a missing
    multiplier -> True)."""
    if kind not in PDT_KINDS:
        return False
    if state is None:
        return True
    multiplier = _dec(state.get("multiplier"))
    if multiplier is not None and multiplier <= 1:
        return False
    values = [v for v in (_dec(state.get("equity")), _dec(state.get("last_equity")))
              if v is not None]
    return not values or min(values) < PDT_EQUITY_MIN


def _window(now: datetime) -> list[date]:
    return last_sessions(PDT_MARKET, now, PDT_SESSIONS)


async def _day_trades(
    session: AsyncSession, now: datetime, *, link_id: uuid.UUID | None = None,
    portfolio_id: uuid.UUID | None = None,
) -> dict[date, int]:
    """Day trades per session of the window, from the fills of the orders of a link or of a
    portfolio on us_equity instruments."""
    days = _window(now)
    if not days:
        return {}
    start = session_on(PDT_MARKET, days[0])[0]
    q = (select(Fill.order_id, Fill.symbol, Fill.side, Fill.qty, Fill.created_at)
         .join(Order, Order.id == Fill.order_id)
         .join(Instrument, Instrument.symbol == Fill.symbol)
         .where(Instrument.asset_class == AssetClass.us_equity, Fill.created_at >= start))
    if link_id is not None:
        q = q.where(Order.broker_link_id == link_id)
    if portfolio_id is not None:
        q = q.where(Fill.portfolio_id == portfolio_id)
    rows = [FillRow(o, s, side, qty, local_date(PDT_MARKET, ts), ts)
            for o, s, side, qty, ts in (await session.execute(q)).all()]
    return {d: n for d, n in count_day_trades(rows).items() if d in days}


async def _pending_pairs(
    session: AsyncSession, now: datetime, portfolio_ids: list[uuid.UUID]
) -> set[tuple[uuid.UUID, str]]:
    """(portfolio, symbol) pairs that will cost an operation: an open day position, or a
    resting `day_trade` buy (not reduce-only) without one."""
    if not portfolio_ids:
        return set()
    pairs: set[tuple[uuid.UUID, str]] = set()
    for pid in portfolio_ids:
        pairs |= {(pid, s) for s in await day_quantities(session, pid, now)}
    resting = (await session.execute(
        select(Order.portfolio_id, Order.symbol)
        .join(Instrument, Instrument.symbol == Order.symbol)
        .where(Order.portfolio_id.in_(portfolio_ids), Order.side == OrderSide.buy,
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
    return session_after(PDT_MARKET, min(busy), PDT_SESSIONS) if busy else None


async def _link_budget(
    session: AsyncSession, link_id: uuid.UUID, now: datetime
) -> tuple[PdtBudget, set[tuple[uuid.UUID, str]]]:
    link = await session.get(BrokerLink, link_id)
    if link is None:
        raise ValueError(f"broker link {link_id} not found")
    applies = pdt_applies(link.kind, link.pdt_state)
    per_day = await _day_trades(session, now, link_id=link_id)
    used = sum(per_day.values())
    pids = list((await session.execute(
        select(Portfolio.id).where(Portfolio.broker_link_id == link_id))).scalars())
    pairs = await _us_pairs(session, await _pending_pairs(session, now, pids))
    limit = PDT_MAX if applies else None
    remaining = None if limit is None else max(0, limit - used - len(pairs))
    raw = (link.pdt_state or {}).get("daytrade_count")
    try:
        broker_count = None if raw is None else int(raw)
    except (TypeError, ValueError):
        broker_count = None
    return PdtBudget(
        applies=applies, limit=limit, used=used, pending=len(pairs), remaining=remaining,
        frees_on=_frees_on(per_day), broker_count=broker_count,
        mismatch=broker_count is not None and broker_count != used,
    ), pairs


async def link_budget(session: AsyncSession, link_id: uuid.UUID, now: datetime) -> PdtBudget:
    """The day-trade budget of a broker link: every portfolio and both modes together."""
    return (await _link_budget(session, link_id, now))[0]


def _release(b: PdtBudget) -> PdtBudget:
    """The budget with one pending operation released (the symbol is already counted)."""
    if b.pending == 0 or b.limit is None:
        return b
    pending = b.pending - 1
    return replace(b, pending=pending, remaining=max(0, b.limit - b.used - pending))


async def entry_budget(
    session: AsyncSession, portfolio: Portfolio, symbol: str, now: datetime
) -> PdtBudget:
    """What a new `day_trade` buy of `symbol` can spend: the lower of the link budget (when
    the link is a PDT broker) and the portfolio's own cap `max_day_trades_5d`. A symbol that
    already has a day position or a resting day-trade buy adds no future operation."""
    candidates: list[PdtBudget] = []
    if portfolio.broker_link_id is not None:
        link = await session.get(BrokerLink, portfolio.broker_link_id)
        if link is not None and link.kind in PDT_KINDS:
            b, pairs = await _link_budget(session, link.id, now)
            candidates.append(_release(b) if (portfolio.id, symbol) in pairs else b)
    cap = portfolio.max_day_trades_5d
    if cap is not None:
        per_day = await _day_trades(session, now, portfolio_id=portfolio.id)
        used = sum(per_day.values())
        pairs = await _us_pairs(session, await _pending_pairs(session, now, [portfolio.id]))
        pending = len(pairs) - (1 if (portfolio.id, symbol) in pairs else 0)
        candidates.append(PdtBudget(
            applies=True, limit=cap, used=used, pending=pending,
            remaining=max(0, cap - used - pending), frees_on=_frees_on(per_day),
            broker_count=None, mismatch=False,
        ))
    if not candidates:
        return PdtBudget(False, None, 0, 0, None, None, None, False)
    limited = [c for c in candidates if c.remaining is not None]
    if not limited:
        return candidates[0]
    return min(limited, key=lambda c: c.remaining)
