"""BrokerSync: the only component that talks to brokers about orders; reconciliation.

The engine never calls a broker (no network I/O under the portfolio lock): it marks an
order `broker_status` (engine.models.BrokerState) and BrokerSync carries it out every
`interval_s`, each order in its own transactions:

- `to_send`: once the order's market is open, the local checks run again (locked
  portfolio, kill switch for orders that are not reduce_only); the order is claimed
  (`sending`, committed) and only then sent, outside any transaction. A broker rejection
  rejects the order with PROVIDER_UNAVAILABLE and the broker's message, and notifies.
  An order the adapter cannot take after `prepare` (or whose link was disabled or
  removed) is released to the simulator (OrderProcessor).
- a failed send: retried on the next pass when the adapter is idempotent (Alpaca
  client_order_id); otherwise (Trading 212, Ruling R7) it becomes `unknown`, is never
  sent again, and `find_order` looks for it; after MATCH_WINDOW without a match it is
  rejected locally (or canceled, if a cancel was requested) and left to reconciliation.
  A `sending` order found at startup (crash mid-send) is treated the same way.
- live orders: `get_order`; a growth of filled_qty becomes a broker fill of the delta at
  `(avg_new * filled_new - avg_old * filled_old) / delta`. Without a broker price for a
  partial fill (Trading 212 pending orders) the fill waits for one. Final states
  `canceled`, `rejected`, `expired` are applied to the order.
- `cancel_requested`: cancel at the broker, read the order back, apply the last fills,
  then the final state; the order stays open until the broker confirms.
- `replace_requested` (modify): cancel at the broker; once canceled without fills the
  order is sent again with the new parameters and a new client reference
  (broker_attempt + 1); if it filled meanwhile, the fills stand and the rest is canceled.

`reconcile` compares the broker's positions with ours; it never corrects anything.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.base import BrokerAdapter, BrokerOrder
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.service import get_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import activate_linked, execute_fill
from smtrading.engine.models import (
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderStatus,
    OrderType,
)
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import cancel_held_children
from smtrading.events import (
    discard_post_commit,
    dispatch_post_commit,
    notify,
    queue_event,
    queue_untrack,
)
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.daily_loss import check_daily_loss_safely
from smtrading.risk.settings import is_kill_switch_on

log = logging.getLogger("smtrading.brokers.sync")

SYNC_INTERVAL_S = 2.0
MATCH_WINDOW = timedelta(minutes=5)
_SYNCED = tuple(s for s in OrderStatus if s in OPEN_STATUSES - {OrderStatus.held})
_FINAL = {"canceled": OrderStatus.canceled, "rejected": OrderStatus.rejected,
          "expired": OrderStatus.expired}
_PREC = 60


def delta_price(order: Order, bo: BrokerOrder, delta: Decimal) -> Decimal:
    """Price of the newly filled `delta`, from the broker's and our average prices."""
    assert bo.avg_fill_price is not None
    with localcontext() as ctx:
        ctx.prec = _PREC
        before = order.filled_qty or Decimal(0)
        old_avg = order.avg_fill_price or Decimal(0)
        price = (bo.avg_fill_price * bo.filled_qty - old_avg * before) / delta
        price = price.quantize(Decimal("1e-18")).normalize()
    return price if price > 0 else bo.avg_fill_price


async def _lock(session: AsyncSession, order_id: UUID) -> Order | None:
    """Portfolio row first, then the order row (Ruling R11)."""
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        return None
    await lock_portfolio(session, portfolio_id)
    return (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _untrack_if_idle(session: AsyncSession, symbol: str) -> None:
    await session.flush()
    still_open = await session.scalar(
        select(Order.id).where(Order.symbol == symbol, Order.status.in_(OPEN_STATUSES)).limit(1)
    )
    if still_open is None:
        queue_untrack(session, symbol, "orders")


class BrokerSync:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis,
        qs: QuoteSource,
        http,
        *,
        interval_s: float = SYNC_INTERVAL_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._qs = qs
        self._http = http
        self._interval_s = interval_s
        self._clock = clock
        self._adapters: dict[UUID, BrokerAdapter] = {}
        self._started = False

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("broker sync pass failed")
            await asyncio.sleep(self._interval_s)

    async def tick(self) -> None:
        """One pass over the open broker-managed orders, oldest first."""
        self._adapters = {}  # configs may change between passes
        async with self._sm() as session:
            rows = (await session.execute(
                select(Order.id, Order.broker_status, Order.broker_order_id)
                .where(Order.broker_link_id.is_not(None), Order.status.in_(_SYNCED))
                .order_by(Order.created_at, Order.id)
            )).all()
        first, self._started = not self._started, True
        for order_id, status, broker_order_id in rows:
            try:
                if status == BrokerState.TO_SEND or (status == BrokerState.SENDING and first):
                    await self._send(order_id)
                elif status == BrokerState.SENDING:
                    continue  # another pass is sending it
                elif broker_order_id is None:
                    await self._match(order_id)
                elif status in (BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED):
                    await self._cancel(order_id)
                else:
                    await self._poll(order_id)
            except Exception:
                log.exception("broker sync of order %s failed", order_id)

    # -- helpers ---------------------------------------------------------------------

    async def _adapter(self, session: AsyncSession, link_id: UUID) -> BrokerAdapter:
        if link_id not in self._adapters:
            self._adapters[link_id] = await get_adapter(session, link_id, self._http)
        return self._adapters[link_id]

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()
        await dispatch_post_commit(session, self._redis)

    async def _reject(
        self, session: AsyncSession, order: Order, code: ErrorCode, message: str | None
    ) -> None:
        order.status = OrderStatus.rejected
        order.reject_code = str(code)
        order.reject_message = (message or "")[:512]
        order.updated_at = datetime.now(UTC)
        await cancel_held_children(session, order, by="system")
        await notify(
            session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
            kind="order.rejected",
            payload={
                "order_id": order.id, "symbol": order.symbol, "code": str(code),
                "message": message, "broker": True,
            },
        )
        await _untrack_if_idle(session, order.symbol)
        log.warning("order %s rejected: %s %s", order.id, code, message)

    def _set_final(self, session: AsyncSession, order: Order, status: OrderStatus) -> None:
        order.status = status
        order.updated_at = datetime.now(UTC)
        queue_event(session, f"order.{status}", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol, "by": "broker",
        })

    def _release(self, session: AsyncSession, order: Order) -> None:
        """Back to the simulator (OrderProcessor); a requested cancel just cancels."""
        canceling = order.broker_status == BrokerState.CANCEL_REQUESTED
        order.broker_link_id = None
        order.broker_status = None
        order.broker_replicated = False
        if canceling:
            self._set_final(session, order, OrderStatus.canceled)
            return
        if order.type == OrderType.market and order.status == OrderStatus.pending:
            order.status = OrderStatus.queued  # executed by the processor's sweep
        order.updated_at = datetime.now(UTC)
        log.info("order %s released to the simulator", order.id)

    async def _send_refusal(
        self, session: AsyncSession, portfolio: Portfolio, order: Order
    ) -> SMTError | None:
        """The local checks again, right before sending."""
        if portfolio.status == PortfolioStatus.locked:
            return SMTError(ErrorCode.PORTFOLIO_LOCKED, "portfolio is locked")
        if portfolio.status == PortfolioStatus.reduce_only and not order.reduce_only:
            return SMTError(ErrorCode.REDUCE_ONLY, "portfolio is reduce-only")
        if not order.reduce_only and await is_kill_switch_on(session):
            return SMTError(ErrorCode.KILL_SWITCH_ACTIVE, "trading is halted by the kill switch")
        return None

    async def _mark_unknown(self, session: AsyncSession, order: Order, reason: str) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.UNKNOWN
        order.updated_at = datetime.now(UTC)
        await notify(
            session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
            kind="broker.order_unknown",
            payload={"order_id": order.id, "symbol": order.symbol, "reason": reason},
        )
        log.error("order %s: broker outcome unknown (%s); not sent again", order.id, reason)

    # -- send ------------------------------------------------------------------------

    async def _send(self, order_id: UUID) -> None:
        now = self._clock()
        async with self._sm() as session:
            order = await _lock(session, order_id)
            if order is None or order.status not in _SYNCED or order.broker_status not in (
                BrokerState.TO_SEND, BrokerState.SENDING
            ):
                return
            portfolio = await session.get(Portfolio, order.portfolio_id)
            instrument = await session.get(Instrument, order.symbol)
            link = await session.get(BrokerLink, order.broker_link_id)
            if order.broker_status == BrokerState.SENDING:  # left over by a crash
                adapter = await self._adapter(session, order.broker_link_id)
                if not adapter.idempotent_place:
                    await self._mark_unknown(session, order, "interrupted while sending")
                    await self._commit(session)
                    return
                order.broker_status = BrokerState.TO_SEND
            if order.expires_at is not None and order.expires_at <= now:
                self._set_final(session, order, OrderStatus.expired)
                await cancel_held_children(session, order, by="system")
                await self._commit(session)
                return
            if (
                link is None or not link.enabled
                or portfolio.broker_link_id != order.broker_link_id
            ):
                self._release(session, order)
                await self._commit(session)
                return
            if not market_status(instrument.market, now).is_open:
                await self._commit(session)
                return
            refusal = await self._send_refusal(session, portfolio, order)
            if refusal is not None:
                await self._reject(session, order, refusal.code, refusal.message)
                await self._commit(session)
                return
            adapter = await self._adapter(session, link.id)
            order.broker_status = BrokerState.SENDING
            order.broker_last_sync_at = now
            await self._commit(session)  # claimed before any network I/O

        try:
            await adapter.prepare([instrument])
            supported = adapter.supports(order, instrument)
        except RuntimeError as e:  # nothing was sent: try again next pass
            log.warning("order %s: broker not ready: %s", order_id, e)
            await self._update(order_id, self._unclaim)
            return
        if not supported:
            await self._update(order_id, self._release_if_claimed)
            return
        try:
            bo = await adapter.place_order(order, instrument)
        except RuntimeError as e:
            reason = str(e)
            log.warning("order %s: send failed: %s", order_id, reason)
            if adapter.idempotent_place:
                await self._update(order_id, self._unclaim)
            else:
                await self._update(
                    order_id, lambda s, o: self._mark_unknown(s, o, reason)
                )
            return
        await self._update(order_id, lambda s, o: self._placed(s, o, bo))

    async def _update(self, order_id: UUID, fn) -> None:
        """Run `fn(session, locked order)` in its own transaction (open orders only)."""
        async with self._sm() as session:
            try:
                order = await _lock(session, order_id)
                if order is None or order.status not in _SYNCED:
                    return
                await fn(session, order)
                await session.commit()
            except BaseException:
                await session.rollback()
                discard_post_commit(session)
                raise
            await dispatch_post_commit(session, self._redis)

    async def _unclaim(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.TO_SEND

    async def _release_if_claimed(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status in (BrokerState.SENDING, BrokerState.CANCEL_REQUESTED):
            self._release(session, order)

    async def _placed(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if bo.status == "rejected":
            await self._reject(
                session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                bo.message or "rejected by the broker",
            )
            return
        order.broker_order_id = bo.broker_order_id
        order.broker_replicated = True
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = bo.status
        queue_event(session, "order.broker_accepted", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol,
            "broker_order_id": bo.broker_order_id,
        })
        await self._apply(session, order, bo)

    # -- live orders -----------------------------------------------------------------

    async def _read(self, order_id: UUID) -> tuple[Order, Instrument, BrokerAdapter] | None:
        """The order, its instrument and adapter, read without locks (no transaction is
        kept open across the network call that follows)."""
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if order is None or order.broker_link_id is None or order.status not in _SYNCED:
                return None
            instrument = await session.get(Instrument, order.symbol)
            adapter = await self._adapter(session, order.broker_link_id)
            await session.commit()
        return order, instrument, adapter

    async def _poll(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, _, adapter = read
        bo = await adapter.get_order(order.broker_order_id)
        await self._update(order_id, lambda s, o: self._apply_same(s, o, bo))

    async def _cancel(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, _, adapter = read
        await adapter.cancel_order(order.broker_order_id)
        bo = await adapter.get_order(order.broker_order_id)
        await self._update(order_id, lambda s, o: self._apply_same(s, o, bo))

    async def _apply_same(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id == bo.broker_order_id:  # not resubmitted meanwhile
            await self._apply(session, order, bo)

    async def _apply(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """Apply the broker's view of the order (caller holds the locks)."""
        order.broker_last_sync_at = self._clock()
        delta = bo.filled_qty - (order.filled_qty or Decimal(0))
        if delta > 0:
            if bo.avg_fill_price is None:
                return  # no broker price yet: wait for it
            price = delta_price(order, bo, delta)
            await execute_fill(
                session, self._qs, order.id, fill_qty=delta, fill_price=price,
                source="broker", final=bo.status == "filled", redis=self._redis,
            )
            await check_daily_loss_safely(
                session, self._qs, order.portfolio_id, self._clock(), redis=self._redis
            )
        if order.status not in _SYNCED:  # filled by execute_fill
            order.broker_status = bo.status
            await _untrack_if_idle(session, order.symbol)
            return
        if bo.status == "filled":  # e.g. a notional order, or the broker filled less
            order.status = OrderStatus.filled
            order.broker_status = bo.status
            order.updated_at = datetime.now(UTC)
            await activate_linked(session, order)
            await _untrack_if_idle(session, order.symbol)
            return
        if bo.status in _FINAL:
            await self._finish(session, order, bo)
            return
        if order.broker_status not in (
            BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED
        ):
            order.broker_status = bo.status

    async def _finish(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        replacing = order.broker_status == BrokerState.REPLACE_REQUESTED
        if replacing and bo.status == "canceled" and order.filled_qty == 0:
            order.broker_order_id = None
            order.broker_replicated = False
            order.broker_attempt = (order.broker_attempt or 0) + 1
            order.broker_status = BrokerState.TO_SEND
            order.updated_at = datetime.now(UTC)
            return
        order.broker_status = bo.status
        if bo.status == "rejected":
            await self._reject(
                session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                bo.message or "rejected by the broker",
            )
            return
        if replacing:
            order.reject_message = "replace failed: the order filled before the cancel"
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="order.replace_failed",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "filled_qty": order.filled_qty},
            )
        self._set_final(session, order, _FINAL[bo.status])
        if order.filled_qty > 0:  # a partly filled entry: its exits cover what filled
            for child in await session.scalars(
                select(Order).where(Order.parent_id == order.id,
                                    Order.status == OrderStatus.held)
            ):
                child.qty = order.filled_qty
            await activate_linked(session, order)
        else:
            await cancel_held_children(session, order, by="system")
        await _untrack_if_idle(session, order.symbol)

    # -- unknown outcome (Ruling R7) -------------------------------------------------

    async def _match(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, instrument, adapter = read
        since = order.broker_last_sync_at or order.updated_at
        bo = await adapter.find_order(order, instrument, since)
        if bo is not None:
            await self._update(order_id, lambda s, o: self._matched(s, o, bo))
            return
        if self._clock() - since > MATCH_WINDOW:
            await self._update(order_id, self._give_up)

    async def _matched(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id is not None:
            return
        order.broker_order_id = bo.broker_order_id
        order.broker_replicated = True
        if order.broker_status == BrokerState.UNKNOWN:
            order.broker_status = bo.status
        log.warning("order %s matched broker order %s", order.id, bo.broker_order_id)
        await self._apply(session, order, bo)

    async def _give_up(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is not None:
            return
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            self._set_final(session, order, OrderStatus.canceled)
            await cancel_held_children(session, order, by="system")
            await _untrack_if_idle(session, order.symbol)
            return
        await self._reject(
            session, order, ErrorCode.PROVIDER_UNAVAILABLE,
            "broker outcome unknown: no matching broker order found; check the reconciliation",
        )


# -- reconciliation --------------------------------------------------------------------


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f") if v != 0 else "0"


async def _position_balances(session: AsyncSession, portfolio_id: UUID) -> dict[str, Decimal]:
    rows = await session.execute(
        select(Account.asset, AccountBalance.balance)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id, Account.kind == AccountKind.position,
            AccountBalance.balance != 0,
        )
    )
    return {asset: balance for asset, balance in rows}


async def reconcile(session: AsyncSession, link_id: UUID, http) -> list[Reconciliation]:
    """Compare the broker's positions with ours and store one Reconciliation per linked
    portfolio (diffs: [{symbol, ours, broker}], quantities as strings). Portfolios that
    share a link share the broker account, so `ours` is their sum. Nothing is
    corrected. The caller's transaction is committed before the broker is called (no
    transaction stays open across network I/O); the caller commits the rows."""
    link = await session.get(BrokerLink, link_id)
    if link is None:
        raise SMTError(ErrorCode.NOT_FOUND, f"broker link {link_id} not found")
    portfolio_ids = list(await session.scalars(
        select(Portfolio.id).where(Portfolio.broker_link_id == link_id).order_by(Portfolio.id)
    ))
    if not portfolio_ids:
        return []
    ours: dict[str, Decimal] = {}
    for pid in portfolio_ids:
        for symbol, qty in (await _position_balances(session, pid)).items():
            ours[symbol] = ours.get(symbol, Decimal(0)) + qty
    instruments = list(await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(sorted(ours)))
    )) if ours else []
    adapter = await get_adapter(session, link_id, http)
    await session.commit()  # no transaction kept open across the network calls
    await adapter.prepare(instruments)
    broker = await adapter.get_positions()
    diffs = [
        {"symbol": symbol, "ours": _num(ours.get(symbol, Decimal(0))),
         "broker": _num(broker.get(symbol, Decimal(0)))}
        for symbol in sorted(set(ours) | set(broker))
        if ours.get(symbol, Decimal(0)) != broker.get(symbol, Decimal(0))
    ]
    rows = [
        Reconciliation(broker_link_id=link_id, portfolio_id=pid, diffs=diffs, ok=not diffs)
        for pid in portfolio_ids
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def reconcile_all(sessionmaker: async_sessionmaker[AsyncSession], http) -> int:
    """Reconcile every enabled link (scheduler job); the link status records the
    outcome. Returns how many reconciliations found differences."""
    async with sessionmaker() as session:
        link_ids = list(await session.scalars(
            select(BrokerLink.id).where(BrokerLink.enabled.is_(True)).order_by(BrokerLink.name)
        ))
    mismatched = 0
    for link_id in link_ids:
        async with sessionmaker() as session:
            try:
                rows = await reconcile(session, link_id, http)
                link = await session.get(BrokerLink, link_id)
                link.status, link.last_error = "ok", None
                await session.commit()
            except Exception as e:  # noqa: BLE001  (any failure goes to the link status)
                await session.rollback()
                log.warning("reconciliation of broker link %s failed: %s", link_id, e)
                link = await session.get(BrokerLink, link_id)
                if link is not None:
                    link.status, link.last_error = "error", str(e)[:500]
                    await session.commit()
                continue
        bad = [r for r in rows if not r.ok]
        if bad:
            mismatched += len(bad)
            log.warning("broker link %s: positions differ: %s", link_id, bad[0].diffs)
    return mismatched
