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
- An exit filled → `closed`, with its reason: a bracket child (`target`/`stop`, expected
  at its price), the rule's own exit (`smt-autox-{trade}`, its reason already written),
  the worker's end-of-session safety close (`smt-dayclose-…` → `day_close`) or any other
  reduce-only sell (`manual`). Exit time (broker's when reported), price and slippage
  (same sign convention as the entry's: `(exit - expected) / expected × 10000`), the
  broker's real fees (the sum of the `Fill.fee_amount` of entry and exit) and the net
  result `(exit - fill) × qty - fees` in the portfolio's currency, at the exchange rate
  the portfolio uses (`qs.get_fx_rate`). The runner is told (`exited`).
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
from smtrading.portfolios.models import Portfolio

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


@dataclass
class _Snapshot:
    items: list[_Item]
    orders: dict[uuid.UUID, Order]
    fills: dict[uuid.UUID, _Fills]


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
        self._executor = executor or Executor(sessionmaker, redis, qs, clock=clock)
        self._open: dict[uuid.UUID, AutoTrade] = {}  # rule -> its open trade, last step
        self.active = False  # a trade was `sent` or `open` at the last step
        self._dead_exits: set[int] = set()  # exit orders ended unfilled, logged once

    def open_trade(self, rule_id: uuid.UUID) -> AutoTrade | None:
        """The rule's open trade at the last step (the exits' target)."""
        return self._open.get(rule_id)

    async def step(self, runners: dict) -> None:
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
            active = active or trade.status in (SENT, OPEN)
        self._open, self.active = still_open, active

    # --- the read --------------------------------------------------------------------

    async def _read(self) -> _Snapshot | None:
        async with self._sm() as s:
            rows = (await s.execute(
                select(AutoTrade, AutoRule.model, AutoRuleVersion.params, Instrument.currency,
                       Portfolio.base_currency)
                .join(AutoRule, AutoRule.id == AutoTrade.rule_id)
                .join(AutoRuleVersion, (AutoRuleVersion.rule_id == AutoTrade.rule_id)
                      & (AutoRuleVersion.version == AutoTrade.rule_version))
                .join(Instrument, Instrument.symbol == AutoTrade.symbol)
                .join(Portfolio, Portfolio.id == AutoTrade.portfolio_id)
                .where(AutoTrade.status.in_((SENT, OPEN)))
                .order_by(AutoTrade.id))).all()
            if not rows:
                return None
            items = [_Item(t, _timeout(model, params), ccy, base)
                     for t, model, params, ccy, base in rows]
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
        return _Snapshot(items, orders, fills)

    # --- one trade -------------------------------------------------------------------

    async def _track(self, item: _Item, snap: _Snapshot, runner, now: datetime) -> AutoTrade:
        trade = item.trade
        if trade.status == SENT:
            trade = await self._sent(item, snap, runner, now)
            if trade.status != OPEN:
                return trade
        if runner is not None and runner.state != "long":
            runner.entered(trade.filled_at, trade.fill_price)  # a restart: rebuilt
        found = self._exit_of(trade, snap)
        if found is None:
            own = snap.orders.get(trade.exit_order_id)
            if own is not None and _final(own) and own.filled_qty == 0:
                if trade.id not in self._dead_exits:
                    log.warning("automatic trade %s: exit order %s ended unfilled (%s); a "
                                "new one goes at the next exit", trade.id, own.id, own.status)
                    self._dead_exits.add(trade.id)
                self._executor.exit_failed(trade.id)
            return trade
        return await self._close(item, snap, found, runner)

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
        if (entry.type == OrderType.limit and entry.status in FILLABLE_STATUSES
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
    def _exit_of(trade: AutoTrade, snap: _Snapshot) -> _Exit | None:
        """The order that closed the trade (filled and final), the first to fill."""
        entry = snap.orders.get(trade.entry_order_id)
        found: list[tuple[datetime, _Exit]] = []

        def add(o: Order, reason: str, expected: Decimal | None) -> None:
            if o.filled_qty > 0 and _final(o) and o.avg_fill_price is not None:
                first = snap.fills.get(o.id, _Fills()).first_at or o.updated_at
                found.append((first, _Exit(o, reason, expected)))

        mine = {trade.exit_order_id}
        for o in snap.orders.values():
            if entry is not None and o.parent_id == entry.id and o.role in CHILD_REASONS:
                mine.add(o.id)
                add(o, CHILD_REASONS[o.role],
                    o.limit_price if o.type == OrderType.limit else o.stop_price)
        own = snap.orders.get(trade.exit_order_id)
        if own is not None:
            add(own, trade.exit_reason or "manual", trade.exit_expected)
        for o in snap.orders.values():
            if (o.id in mine or o.portfolio_id != trade.portfolio_id
                    or o.symbol != trade.symbol or not o.reduce_only
                    or o.side != OrderSide.sell
                    or (entry is not None and o.created_at < entry.created_at)):
                continue
            day_close = (o.client_order_id or "").startswith(DAY_CLOSE_PREFIX)
            add(o, "day_close" if day_close else "manual", o.limit_price)
        if not found:
            return None
        return min(found, key=lambda f: f[0])[1]

    async def _close(self, item: _Item, snap: _Snapshot, ex: _Exit, runner) -> AutoTrade:
        trade = item.trade
        base = item.base_currency
        fees: dict[str, Decimal] = {}
        for order_id in (trade.entry_order_id, ex.order.id):
            for ccy, amount in snap.fills.get(order_id, _Fills()).fees.items():
                fees[ccy] = fees.get(ccy, Decimal(0)) + amount
        # the exchange rates with no session open
        rate = await self.qs.get_fx_rate(item.currency, base)
        fee_rates = {ccy: await self.qs.get_fx_rate(ccy, base) for ccy in fees}
        exit_price = ex.order.avg_fill_price
        gross = (exit_price - trade.fill_price) * trade.qty
        fees_base = sum((a * fee_rates[c] for c, a in fees.items()), Decimal(0))
        if len(fees) == 1:
            [(fees_ccy, fees_total)] = fees.items()
        else:  # none, or several currencies: in the portfolio's
            fees_ccy, fees_total = (base if fees else None), fees_base
        # the broker's time, written with the fill, belongs to this exit
        stamped = ex.order.id == trade.exit_order_id or (
            trade.entry_order_id is not None and ex.order.parent_id == trade.entry_order_id)
        async with self._sm() as s:
            row = await self._locked(s, trade, OPEN)
            if row is None:
                await s.rollback()
                return trade
            if not stamped or row.exit_filled_at is None:
                row.exit_filled_at = (snap.fills.get(ex.order.id, _Fills()).first_at
                                      or ex.order.updated_at)
            row.exit_order_id = ex.order.id
            row.exit_reason = ex.reason
            row.exit_expected = ex.expected
            row.exit_price = exit_price
            row.exit_slippage_bps = _bps(exit_price, ex.expected)
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
