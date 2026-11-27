"""The Tracker of the `scalper` (intraday mode B): what became of the trades `sent` and
`open`, every TRACK_EVERY_S, read from the orders and fills the engine and BrokerSync
wrote (it never talks to a broker).

- An entry filled → `open`: the fill time is the broker's (`fill_ts_source = "broker"`,
  written by BrokerSync with the fill, `autorules.broker_times`), otherwise the time of
  its first Fill (`"local"`); fill price, quantity and the time BrokerSync sent it come
  from the order; the entry slippage `(fill - expected) / expected × 10000` and the
  latencies in ms. The runner is told (`entered`).
- An entry that ended unfilled → `canceled` (`rejected` when the broker refused it), the
  runner back to flat (`canceled`): the rule is never left busy. A limit entry not filled
  within the rule's `entry_timeout_s` is canceled (`Executor.cancel_stale_entry`).
- Exits filled → `closed` once they sold the whole trade (or no position is left), with
  the reason of the first: a bracket child (`target`/`stop`, expected at its price), the
  rule's own exit (`smt-autox-{trade}` and its replacements, reason already written),
  the worker's end-of-session safety close (`smt-dayclose-…` → `day_close`) or any other
  reduce-only sell (`manual`). Exit time (broker's when reported), average price on the
  filled quantities and slippage (same sign convention as the entry's: `(exit -
  expected) / expected × 10000`), the broker's real fees (the sum of the
  `Fill.fee_amount` of entry and exits) and the net result `Σ (exit - fill) × filled -
  fees` (on the trade's quantity at most) in the portfolio's currency, at the exchange
  rate the portfolio uses (`qs.get_fx_rate`). The runner is told (`exited`). A partial exit keeps the trade
  open; the rule's own exit that ended short is replaced (`Executor.exit_failed`). The
  trades closed at a step are in `closed` (the scalper judges the brakes on them).
- A restarted scalper has new runners: an `open` trade puts its runner back in `long`
  with the saved fill (time and end-of-session exits go on at the right time), a `sent`
  one puts it in `pending`.

One read (a session closed before anything else), the exchange rates with no session
open, then one short transaction per change, the trade row locked and its status
checked again. `step` never raises: a trade that fails is logged and tried at the next
step."""
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.broker_times import auto_exit_client_id
from smtrading.autorules.executor import Executor, _ms
from smtrading.autorules.models import validate_params
from smtrading.autorules.tables import (
    CANCELED,
    CLOSED,
    OPEN,
    REJECTED,
    SENT,
    AutoRule,
    AutoRuleVersion,
    AutoTrade,
)
from smtrading.common.errors import SMTError
from smtrading.engine.models import (
    FILLABLE_STATUSES,
    OPEN_STATUSES,
    BrokerState,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import floor_to_lot
from smtrading.portfolios.models import Portfolio
from smtrading.positions.models import Position

log = logging.getLogger(__name__)

TRACK_EVERY_S = 0.5
DAY_CLOSE_PREFIX = "smt-dayclose-"
CHILD_REASONS = {"take_profit": "target", "stop_loss": "stop"}
DEFAULT_ENTRY_TIMEOUT_S = 5
_BPS = Decimal(10000)


@dataclass
class _Fills:
    first_at: datetime | None = None
    fees: dict[str, Decimal] = field(default_factory=dict)  # currency -> amount


@dataclass
class _Item:
    trade: AutoTrade
    entry_timeout_s: int
    currency: str  # the instrument's
    base_currency: str  # the portfolio's
    lot: Decimal | None = None  # the instrument's lot size


@dataclass
class _Snapshot:
    items: list[_Item]
    orders: dict[uuid.UUID, Order]
    fills: dict[uuid.UUID, _Fills]
    positions: dict[tuple[uuid.UUID, str], Decimal]


@dataclass
class _Exit:
    order: Order
    reason: str
    expected: Decimal | None


def _bps(price: Decimal, expected: Decimal | None) -> Decimal | None:
    if expected is None or expected == 0 or price is None:
        return None
    return (price - expected) / expected * _BPS


def _final(order: Order) -> bool:
    return order.status not in OPEN_STATUSES


def _timeout(model: str, params: dict) -> int:
    try:
        return int(validate_params(model, params)["entry_timeout_s"])
    except (SMTError, KeyError, TypeError, ValueError):
        return DEFAULT_ENTRY_TIMEOUT_S


class Tracker:
    def __init__(self, sessionmaker, redis, qs, *, clock: Callable[[], datetime],
                 executor: Executor | None = None) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self.qs = qs
        self._clock = clock
        # None (the scalper without an executor): nothing is canceled or sent from here
        self._executor = executor
        self._open: dict[uuid.UUID, AutoTrade] = {}  # rule -> its open trade, last step
        self.active = False  # a trade was `sent` or `open` at the last step
        self.closed: list[AutoTrade] = []  # the trades closed at the last step
        self._short_exits: set[uuid.UUID] = set()  # exit orders ended short, logged once

    def open_trade(self, rule_id: uuid.UUID) -> AutoTrade | None:
        """The rule's open trade at the last step (the exits' target)."""
        return self._open.get(rule_id)

    async def step(self, runners: dict) -> None:
        self.closed = []
        try:
            snap = await self._read()
        except Exception:
            log.exception("automatic trades: read failed")
            return
        if snap is None:
            self._open, self.active = {}, False
            return
        now = self._clock()
        still_open: dict[uuid.UUID, AutoTrade] = {}
        active = False
        for item in snap.items:
            trade = item.trade
            try:
                trade = await self._track(item, snap, runners.get(trade.rule_id), now)
            except Exception:
                log.exception("automatic trade %s: tracking failed; next step", trade.id)
            if trade.status == OPEN:
                still_open[trade.rule_id] = trade
            elif trade.status == CLOSED:
                self.closed.append(trade)
            active = active or trade.status in (SENT, OPEN)
        self._open, self.active = still_open, active

    # --- the read --------------------------------------------------------------------

    async def _read(self) -> _Snapshot | None:
        async with self._sm() as s:
            rows = (await s.execute(
                select(AutoTrade, AutoRule.model, AutoRuleVersion.params, Instrument.currency,
                       Portfolio.base_currency, Instrument.lot_size)
                .join(AutoRule, AutoRule.id == AutoTrade.rule_id)
                .join(AutoRuleVersion, (AutoRuleVersion.rule_id == AutoTrade.rule_id)
                      & (AutoRuleVersion.version == AutoTrade.rule_version))
                .join(Instrument, Instrument.symbol == AutoTrade.symbol)
                .join(Portfolio, Portfolio.id == AutoTrade.portfolio_id)
                .where(AutoTrade.status.in_((SENT, OPEN)))
                .order_by(AutoTrade.id))).all()
            if not rows:
                return None
            items = [_Item(t, _timeout(model, params), ccy, base, lot)
                     for t, model, params, ccy, base, lot in rows]
            trades = [i.trade for i in items]
            entries = [t.entry_order_id for t in trades if t.entry_order_id is not None]
            exits = [t.exit_order_id for t in trades if t.exit_order_id is not None]
            pairs = {(t.portfolio_id, t.symbol) for t in trades}
            done = select(AutoTrade.exit_order_id).where(
                AutoTrade.status == CLOSED, AutoTrade.exit_order_id.is_not(None))
            orders = {o.id: o for o in await s.scalars(select(Order).where(or_(
                Order.id.in_(entries), Order.parent_id.in_(entries), Order.id.in_(exits),
                and_(Order.reduce_only.is_(True), Order.side == OrderSide.sell,
                     Order.filled_qty > 0, Order.status.not_in(OPEN_STATUSES),
                     tuple_(Order.portfolio_id, Order.symbol).in_(pairs),
                     Order.id.not_in(done))))
            )}
            fills: dict[uuid.UUID, _Fills] = {}
            if orders:
                for order_id, ccy, amount, first in await s.execute(
                    select(Fill.order_id, Fill.fee_currency, Fill.fee_amount,
                           Fill.created_at).where(Fill.order_id.in_(orders))
                ):
                    f = fills.setdefault(order_id, _Fills())
                    if f.first_at is None or first < f.first_at:
                        f.first_at = first
                    f.fees[ccy] = f.fees.get(ccy, Decimal(0)) + (amount or Decimal(0))
            positions = {(p, sym): qty for p, sym, qty in await s.execute(
                select(Position.portfolio_id, Position.symbol, Position.qty).where(
                    tuple_(Position.portfolio_id, Position.symbol).in_(pairs)))}
        return _Snapshot(items, orders, fills, positions)

    # --- one trade -------------------------------------------------------------------

    async def _track(self, item: _Item, snap: _Snapshot, runner, now: datetime) -> AutoTrade:
        trade = item.trade
        if trade.status == SENT:
            trade = await self._sent(item, snap, runner, now)
            if trade.status != OPEN:
                return trade
        if runner is not None and runner.state != "long":
            runner.entered(trade.filled_at, trade.fill_price)  # a restart: rebuilt
        exits = self._exits_of(trade, snap)
        sold = sum((e.order.filled_qty for e in exits), Decimal(0))
        held = snap.positions.get((trade.portfolio_id, trade.symbol), Decimal(0))
        # closed when the exits sold the whole trade or nothing is left (a fee kept in
        # the asset makes the position smaller than the fill); a remainder below one lot
        # (unsold or held: the coin fee's) is nothing left, no order can sell it
        if exits and (floor_to_lot(trade.qty - sold, item.lot) <= 0
                      or floor_to_lot(held, item.lot) <= 0):
            return await self._close(item, snap, exits, runner)
        self._exit_short(trade, snap)
        return trade

    async def _sent(self, item: _Item, snap: _Snapshot, runner, now: datetime) -> AutoTrade:
        trade = item.trade
        entry = snap.orders.get(trade.entry_order_id)
        if entry is not None and entry.filled_qty > 0 and (
                entry.status == OrderStatus.filled or _final(entry)):
            return await self._opened(trade, entry, snap.fills.get(entry.id, _Fills()),
                                      runner)
        if entry is None or _final(entry):
            status = REJECTED if entry is not None and entry.status == OrderStatus.rejected \
                else CANCELED
            return await self._unfilled(trade, entry, status, runner)
        if runner is not None and runner.state == "flat":
            runner.sent(trade.sent_at or trade.trigger_at)  # a restart: rebuilt
        if (self._executor is not None and entry.type == OrderType.limit
                and entry.status in FILLABLE_STATUSES
                and entry.broker_status != BrokerState.CANCEL_REQUESTED
                and trade.sent_at is not None
                and now - trade.sent_at >= timedelta(seconds=item.entry_timeout_s)):
            await self._executor.cancel_stale_entry(trade)
        return trade

    async def _locked(self, s: AsyncSession, trade: AutoTrade, status: str
                      ) -> AutoTrade | None:
        row = await s.get(AutoTrade, trade.id, with_for_update=True, populate_existing=True)
        return row if row is not None and row.status == status else None

    async def _opened(self, trade: AutoTrade, entry: Order, fills: _Fills,
                      runner) -> AutoTrade:
        async with self._sm() as s:
            row = await self._locked(s, trade, SENT)
            if row is None:
                await s.rollback()
                return trade
            if row.fill_ts_source != "broker" or row.filled_at is None:
                row.filled_at = fills.first_at or entry.updated_at
                row.fill_ts_source = "local"
            row.fill_price = entry.avg_fill_price
            row.qty = entry.filled_qty
            row.broker_sent_at = entry.broker_sent_at
            row.entry_slippage_bps = _bps(row.fill_price, row.expected_price)
            if row.broker_sent_at is not None:
                if row.sent_at is not None:
                    row.ms_send_to_broker = _ms(row.broker_sent_at - row.sent_at)
                row.ms_broker_to_fill = _ms(row.filled_at - row.broker_sent_at)
            row.status = OPEN
            await s.commit()
        log.info("automatic trade %s: entry filled at %s", row.id, row.fill_price)
        if runner is not None:
            runner.entered(row.filled_at, row.fill_price)
        return row

    async def _unfilled(self, trade: AutoTrade, entry: Order | None, status: str,
                        runner) -> AutoTrade:
        async with self._sm() as s:
            row = await self._locked(s, trade, SENT)
            if row is None:
                await s.rollback()
                return trade
            row.status = status
            if status == REJECTED and entry is not None and entry.reject_message:
                row.error = f"{entry.reject_code}: {entry.reject_message}"[:200]
            await s.commit()
        log.info("automatic trade %s: entry ended unfilled (%s)", row.id, status)
        if runner is not None and runner.state == "pending":
            runner.canceled()
        return row

    # --- the exit --------------------------------------------------------------------

    @staticmethod
    def _exits_of(trade: AutoTrade, snap: _Snapshot) -> list[_Exit]:
        """Every exit order of the trade that filled and is final, first to fill first: the
        bracket children, the rule's own exits (`smt-autox-{trade}` and its replacements),
        any other reduce-only sell of the symbol placed after the entry."""
        entry = snap.orders.get(trade.entry_order_id)
        own_id = auto_exit_client_id(trade.id)
        found: list[tuple[datetime, _Exit]] = []
        for o in snap.orders.values():
            if not (o.filled_qty > 0 and _final(o) and o.avg_fill_price is not None):
                continue
            coid = o.client_order_id or ""
            if entry is not None and o.parent_id == entry.id and o.role in CHILD_REASONS:
                ex = _Exit(o, CHILD_REASONS[o.role],
                           o.limit_price if o.type == OrderType.limit else o.stop_price)
            elif o.id == trade.exit_order_id or coid == own_id or coid.startswith(
                    f"{own_id}-"):
                ex = _Exit(o, trade.exit_reason or "manual", trade.exit_expected)
            elif (o.portfolio_id == trade.portfolio_id and o.symbol == trade.symbol
                    and o.reduce_only and o.side == OrderSide.sell
                    and (entry is None or o.created_at >= entry.created_at)):
                ex = _Exit(o, "day_close" if coid.startswith(DAY_CLOSE_PREFIX) else "manual",
                           o.limit_price)
            else:
                continue
            found.append((snap.fills.get(o.id, _Fills()).first_at or o.updated_at, ex))
        return [ex for _, ex in sorted(found, key=lambda f: f[0])]

    def _exit_short(self, trade: AutoTrade, snap: _Snapshot) -> None:
        """The rule's exit order ended with less than it asked (refused, canceled, partly
        filled) and the position is still open: the next Exit sends a new one for what
        is left."""
        own = snap.orders.get(trade.exit_order_id)
        if own is None or not _final(own) or own.filled_qty >= (own.qty or Decimal(0)):
            return
        if own.id not in self._short_exits:
            log.warning("automatic trade %s: exit order %s ended with %s of %s (%s); a new "
                        "one goes at the next exit", trade.id, own.id, own.filled_qty,
                        own.qty, own.status)
            self._short_exits.add(own.id)
        if self._executor is not None:
            self._executor.exit_failed(trade.id)

    async def _close(self, item: _Item, snap: _Snapshot, exits: list[_Exit],
                     runner) -> AutoTrade:
        """Closed by `exits` (one, or several partial ones): price and result on the
        quantities they filled, the reason of the first."""
        trade = item.trade
        base = item.base_currency
        first, last = exits[0], exits[-1]
        fees: dict[str, Decimal] = {}
        for order_id in (trade.entry_order_id, *(e.order.id for e in exits)):
            for ccy, amount in snap.fills.get(order_id, _Fills()).fees.items():
                fees[ccy] = fees.get(ccy, Decimal(0)) + amount
        # the exchange rates with no session open
        rate = await self.qs.get_fx_rate(item.currency, base)
        fee_rates = {ccy: await self.qs.get_fx_rate(ccy, base) for ccy in fees}
        sold = sum((e.order.filled_qty for e in exits), Decimal(0))
        proceeds = sum((e.order.avg_fill_price * e.order.filled_qty for e in exits),
                       Decimal(0))
        exit_price = proceeds / sold
        # the trade's own quantity at most: an exit that sold more (a manual close of a
        # position also holding earlier shares) is not counted as the rule's result
        gross = (exit_price - trade.fill_price) * min(sold, trade.qty)
        fees_base = sum((a * fee_rates[c] for c, a in fees.items()), Decimal(0))
        if len(fees) == 1:
            [(fees_ccy, fees_total)] = fees.items()
        else:  # none, or several currencies: in the portfolio's
            fees_ccy, fees_total = (base if fees else None), fees_base
        # the broker's time (written with the fill) belongs to a single exit of the rule
        stamped = len(exits) == 1 and (
            first.order.id == trade.exit_order_id
            or (trade.entry_order_id is not None
                and first.order.parent_id == trade.entry_order_id))
        async with self._sm() as s:
            row = await self._locked(s, trade, OPEN)
            if row is None:
                await s.rollback()
                return trade
            if not stamped or row.exit_filled_at is None:
                row.exit_filled_at = (snap.fills.get(last.order.id, _Fills()).first_at
                                      or last.order.updated_at)
            row.exit_order_id = first.order.id
            row.exit_reason = first.reason
            row.exit_expected = first.expected
            row.exit_price = exit_price
            row.exit_slippage_bps = _bps(exit_price, first.expected)
            row.fees, row.fees_currency = fees_total, fees_ccy
            row.net_result = gross * rate - fees_base
            row.net_currency = base
            row.status = CLOSED
            await s.commit()
        log.info("automatic trade %s closed (%s): %s %s", row.id, row.exit_reason,
                 row.net_result, base)
        if runner is not None:
            runner.exited(row.exit_filled_at)
        return row
