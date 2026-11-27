"""Results per strategy (the «Strategie» page): every fill of the non-archived portfolios
attributed to a strategy, round trips by FIFO lots, P/L and costs in EUR.

- Attribution: the strategy of the order that opens a lot (`order_attributor`, the shared
  rules loaded once per call, no query per order: `PROP-…` client_order_id → the proposal's STR ref; None → «Senza
  strategia»). An exit closes the oldest lots of its portfolio+symbol (FIFO) whatever
  order it comes from (an OCO exit leg carries no proposal) and its P/L goes to the
  closed lots' strategies.
- A trade is one opening order (all its fills); it is closed when all its quantity is.
  A fill bigger than the opposite lots closes them and opens a new trade (flip).
- Costs: the fill's trade fee plus the FX fee of the conversion it caused (the
  `system_fees` lines of its FX journal) and the broker fees booked apart on it (FEE
  journals with its fill id: brokers.fee_activities, Alpaca's regulatory fees); an exit fill's costs are split over the lots it
  closes (and the flip remainder) by quantity.
- EUR: realised P/L (instrument currency) and costs are converted at the current FX rate
  (like the valuation); unrealised P/L = Σ (mark − lot price) × lot qty over
  the trade's open lots (not its average entry) with the portfolio valuation's mark.
  Without a mark or a rate the value is None («—»), never 0.
- P/L are gross; `costs_eur` apart; `total_pnl_eur` = realised + unrealised − costs. A
  closed trade wins when its realised P/L minus its costs is > 0; `win_rate_pct` is over
  the closed trades (None without any).

Corporate actions (splits) change ledger quantities without fills: lots ignore them.
"""

import logging
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import SMTError
from smtrading.common.money import quantize_money
from smtrading.engine.models import Fill, Order, OrderSide
from smtrading.engine.quotesource import QuoteSource
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountKind, Journal, JournalLine, JournalType
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.signals.service import strategy_states
from smtrading.strategies.attribution import order_attributor
from smtrading.valuation.service import value_portfolio

log = logging.getLogger(__name__)

EUR = "EUR"
_PREC = 60


@dataclass(frozen=True)
class FillIn:
    order_id: uuid.UUID
    client_order_id: str | None
    portfolio_id: uuid.UUID
    symbol: str
    currency: str  # the instrument's
    side: OrderSide
    qty: Decimal
    price: Decimal
    ts: datetime
    costs: dict[str, Decimal]  # currency -> amount
    strategy: str | None = None  # of the fill's order (set later on the trades)
    fill_id: uuid.UUID | None = None


@dataclass
class Trade:
    strategy: str | None
    portfolio_id: uuid.UUID
    symbol: str
    currency: str
    direction: str  # long | short
    order_id: uuid.UUID
    client_order_id: str | None
    opened_at: datetime
    closed_at: datetime | None = None
    qty: Decimal = Decimal(0)  # opened
    open_qty: Decimal = Decimal(0)
    open_value: Decimal = Decimal(0)  # Σ lot price × qty of the lots still open
    entry_value: Decimal = Decimal(0)
    exit_qty: Decimal = Decimal(0)
    exit_value: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)  # gross, instrument currency
    costs: dict[str, Decimal] = field(default_factory=dict)
    # (fill, the part of its qty that is this trade's, "entry" | "exit"), in time order
    legs: list[tuple["FillIn", Decimal, str]] = field(default_factory=list)

    @property
    def entry_price(self) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = _PREC
            return self.entry_value / self.qty

    @property
    def exit_price(self) -> Decimal | None:
        if self.exit_qty == 0:
            return None
        with localcontext() as ctx:
            ctx.prec = _PREC
            return self.exit_value / self.exit_qty

    def unrealized(self, mark: Decimal) -> Decimal:
        """Gross P/L of the open lots at `mark` (instrument currency)."""
        sign = 1 if self.direction == "long" else -1
        with localcontext() as ctx:
            ctx.prec = _PREC
            return (mark * self.open_qty - self.open_value) * sign

    def add_costs(self, costs: dict[str, Decimal], part: Decimal, whole: Decimal) -> None:
        for ccy, amount in costs.items():
            if amount:
                self.costs[ccy] = self.costs.get(ccy, Decimal(0)) + amount * part / whole


@dataclass
class _Lot:
    trade: Trade
    qty: Decimal  # signed: > 0 long, < 0 short
    price: Decimal


def fifo_trades(fills: Iterable[FillIn]) -> list[Trade]:
    """Round trips by FIFO lots per portfolio+symbol; fills in time order (stable)."""
    lots: dict[tuple, deque[_Lot]] = defaultdict(deque)
    open_trade: dict[tuple, Trade] = {}  # (portfolio, symbol, order) -> its trade
    trades: list[Trade] = []
    with localcontext() as ctx:
        ctx.prec = _PREC
        for f in sorted(fills, key=lambda f: f.ts):
            key = (f.portfolio_id, f.symbol)
            queue = lots[key]
            left = f.qty if f.side == OrderSide.buy else -f.qty
            while left and queue and (queue[0].qty > 0) != (left > 0):
                lot = queue[0]
                closed = min(abs(lot.qty), abs(left))
                sign = 1 if lot.qty > 0 else -1
                t = lot.trade
                t.realized += (f.price - lot.price) * closed * sign
                t.exit_qty += closed
                t.exit_value += f.price * closed
                t.open_qty -= closed
                t.open_value -= lot.price * closed
                t.add_costs(f.costs, closed, f.qty)
                t.legs.append((f, closed, "exit"))
                lot.qty -= closed * sign
                left += closed * sign
                if lot.qty == 0:
                    queue.popleft()
                if t.open_qty == 0:
                    t.closed_at = f.ts
            if not left:
                continue
            tkey = (f.portfolio_id, f.symbol, f.order_id)
            t = open_trade.get(tkey)
            direction = "long" if left > 0 else "short"
            if t is None or t.open_qty == 0 or t.direction != direction:
                t = Trade(strategy=f.strategy, portfolio_id=f.portfolio_id, symbol=f.symbol,
                          currency=f.currency, direction=direction, order_id=f.order_id,
                          client_order_id=f.client_order_id, opened_at=f.ts)
                open_trade[tkey] = t
                trades.append(t)
            t.qty += abs(left)
            t.open_qty += abs(left)
            t.open_value += f.price * abs(left)
            t.entry_value += f.price * abs(left)
            t.closed_at = None
            t.add_costs(f.costs, abs(left), f.qty)
            t.legs.append((f, abs(left), "entry"))
            queue.append(_Lot(t, left, f.price))
    trades.sort(key=lambda t: (t.opened_at, str(t.portfolio_id), t.symbol))
    return trades


# --- the page ----------------------------------------------------------------------------


@dataclass
class TradeView:
    symbol: str
    direction: str
    order_id: uuid.UUID
    client_order_id: str | None
    opened_at: datetime
    closed_at: datetime | None
    qty: Decimal
    open_qty: Decimal
    currency: str
    entry_price: Decimal
    exit_price: Decimal | None
    mark: Decimal | None
    realized_pnl_eur: Decimal | None
    unrealized_pnl_eur: Decimal | None
    costs_eur: Decimal | None
    pnl_eur: Decimal | None  # realised + unrealised − costs


@dataclass
class StrategyPerformance:
    ref: str | None  # None: «Senza strategia»
    name: str | None
    status: str | None  # the CEO's latest status decision; None when unknown
    portfolio_id: uuid.UUID
    portfolio_name: str
    trades: int
    closed_trades: int
    open_positions: int  # symbols with an open lot
    win_rate_pct: Decimal | None
    realized_pnl_eur: Decimal | None
    unrealized_pnl_eur: Decimal | None
    costs_eur: Decimal | None
    total_pnl_eur: Decimal | None
    first_trade_at: datetime
    last_trade_at: datetime
    trade_list: list[TradeView]


class _Rates:
    """ccy -> EUR at the current rate; None when the rate is unavailable."""

    def __init__(self, qs: QuoteSource) -> None:
        self._qs = qs
        self._rates: dict[str, Decimal | None] = {EUR: Decimal(1)}

    async def eur(self, amount: Decimal, ccy: str) -> Decimal | None:
        if ccy not in self._rates:
            try:
                self._rates[ccy] = await self._qs.get_fx_rate(ccy, EUR)
            except SMTError:
                log.warning("strategy performance: no %s->EUR rate", ccy)
                self._rates[ccy] = None
        rate = self._rates[ccy]
        if rate is None:
            return None
        with localcontext() as ctx:
            ctx.prec = _PREC
            return amount * rate


def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
    total = Decimal(0)
    for v in values:
        if v is None:
            return None
        total += v
    return total


def _money(v: Decimal | None) -> Decimal | None:
    return None if v is None else quantize_money(v, EUR)


async def _fills(session: AsyncSession, portfolio_ids: Iterable[uuid.UUID] | None = None, *,
                 portfolio_id: uuid.UUID | None = None, symbol: str | None = None,
                 ) -> tuple[list[FillIn], dict[uuid.UUID, Portfolio], dict[uuid.UUID, Order]]:
    """Every fill of the non-archived portfolios, or of `portfolio_ids` (archived ones
    included: a report of a concluded campaign), oldest first; only `portfolio_id`'s /
    `symbol`'s when given (FIFO lots are per portfolio+symbol, so a scoped load gives the
    same trades)."""
    where = (Portfolio.status != PortfolioStatus.archived if portfolio_ids is None
             else Portfolio.id.in_(list(portfolio_ids)))
    q = (select(Fill, Order, Portfolio, Instrument.currency)
         .join(Order, Order.id == Fill.order_id)
         .join(Portfolio, Portfolio.id == Fill.portfolio_id)
         .join(Instrument, Instrument.symbol == Fill.symbol)
         .where(where)
         .order_by(Fill.created_at, Fill.id))
    if portfolio_id is not None:
        q = q.where(Fill.portfolio_id == portfolio_id)
    if symbol is not None:
        q = q.where(Fill.symbol == symbol)
    rows = (await session.execute(q)).all()
    if not rows:
        return [], {}, {}
    fx_fees: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(dict)
    for fill_id, asset, amount in (await session.execute(
        select(Journal.fill_id, Account.asset, JournalLine.quantity)
        .join(JournalLine, JournalLine.journal_id == Journal.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(Journal.type.in_((JournalType.FX, JournalType.FEE)),
               Account.kind == AccountKind.system_fees,
               Journal.fill_id.in_([f.id for f, *_ in rows]))
    )).all():
        fx_fees[fill_id][asset] = fx_fees[fill_id].get(asset, Decimal(0)) + amount
    portfolios: dict[uuid.UUID, Portfolio] = {}
    orders: dict[uuid.UUID, Order] = {}
    out = []
    for f, o, p, ccy in rows:
        portfolios[p.id], orders[o.id] = p, o
        costs = dict(fx_fees.get(f.id, {}))
        if f.fee_amount:
            costs[f.fee_currency] = costs.get(f.fee_currency, Decimal(0)) + f.fee_amount
        out.append(FillIn(
            order_id=o.id, client_order_id=o.client_order_id, portfolio_id=p.id,
            symbol=f.symbol, currency=ccy, side=f.side, qty=f.qty, price=f.price,
            ts=f.created_at, costs=costs, fill_id=f.id,
        ))
    return out, portfolios, orders


async def strategy_performance(
    session: AsyncSession, qs: QuoteSource, *, redis=None,
) -> list[StrategyPerformance]:
    """One row per (strategy, portfolio) with fills; by portfolio name, then ref («Senza
    strategia» last)."""
    fills, portfolios, orders = await _fills(session)
    trades = fifo_trades(fills)
    attribute = await order_attributor(session, [orders[t.order_id] for t in trades])
    strategy: dict[uuid.UUID, str | None] = {}
    for t in trades:  # only the opening orders matter
        if t.order_id not in strategy:
            strategy[t.order_id] = await attribute(orders[t.order_id])
        t.strategy = strategy[t.order_id]
    marks: dict[uuid.UUID, dict[str, Decimal]] = {}
    for pid in sorted({t.portfolio_id for t in trades if t.open_qty}, key=str):
        try:
            v = await value_portfolio(session, qs, pid, redis=redis)
        except SMTError as e:
            log.warning("strategy performance: portfolio %s not valued (%s)", pid, e.code)
            continue
        marks[pid] = {p.symbol: p.mark for p in v.positions}
    rates = _Rates(qs)
    states = await strategy_states(session)

    groups: dict[tuple, list[tuple[Trade, TradeView]]] = defaultdict(list)
    for t in trades:
        mark = marks.get(t.portfolio_id, {}).get(t.symbol)
        with localcontext() as ctx:
            ctx.prec = _PREC
            if t.open_qty == 0:
                unreal = Decimal(0)
            elif mark is None:
                unreal = None
            else:
                unreal = await rates.eur(t.unrealized(mark), t.currency)
            real = await rates.eur(t.realized, t.currency)
            costs = _sum([await rates.eur(a, c) for c, a in sorted(t.costs.items())])
            pnl = None if None in (real, unreal, costs) else real + unreal - costs
        groups[(t.strategy, t.portfolio_id)].append((t, TradeView(
            symbol=t.symbol, direction=t.direction, order_id=t.order_id,
            client_order_id=t.client_order_id, opened_at=t.opened_at, closed_at=t.closed_at,
            qty=t.qty, open_qty=t.open_qty, currency=t.currency, entry_price=t.entry_price,
            exit_price=t.exit_price, mark=mark if t.open_qty else None,
            realized_pnl_eur=real, unrealized_pnl_eur=unreal, costs_eur=costs, pnl_eur=pnl,
        )))

    out = []
    for (ref, pid), items in groups.items():
        views = [v for _, v in items]
        closed = [v for t, v in items if t.open_qty == 0]
        known = [v for v in closed if v.realized_pnl_eur is not None and v.costs_eur is not None]
        wins = sum(1 for v in known if v.realized_pnl_eur - v.costs_eur > 0)
        real = _sum(v.realized_pnl_eur for v in views)
        unreal = _sum(v.unrealized_pnl_eur for v in views)
        costs = _sum(v.costs_eur for v in views)
        state = states.get(ref) if ref else None
        times = [v.opened_at for v in views] + [v.closed_at for v in views if v.closed_at]
        out.append(StrategyPerformance(
            ref=ref, name=state.name if state else None,
            status=state.status if state else None,
            portfolio_id=pid, portfolio_name=portfolios[pid].name,
            trades=len(views), closed_trades=len(closed),
            open_positions=len({t.symbol for t, _ in items if t.open_qty}),
            win_rate_pct=(Decimal(wins) * 100 / len(known)).quantize(Decimal("0.1"))
            if known and len(known) == len(closed) else None,
            realized_pnl_eur=_money(real), unrealized_pnl_eur=_money(unreal),
            costs_eur=_money(costs),
            total_pnl_eur=_money(None if None in (real, unreal, costs)
                                 else real + unreal - costs),
            first_trade_at=min(times), last_trade_at=max(times),
            trade_list=[_rounded(v) for v in sorted(views, key=lambda v: v.opened_at,
                                                    reverse=True)],
        ))
    out.sort(key=lambda s: (s.portfolio_name, s.ref is None, s.ref or ""))
    return out


def _rounded(v: TradeView) -> TradeView:
    v.realized_pnl_eur, v.unrealized_pnl_eur = _money(v.realized_pnl_eur), _money(
        v.unrealized_pnl_eur)
    v.costs_eur, v.pnl_eur = _money(v.costs_eur), _money(v.pnl_eur)
    return v
