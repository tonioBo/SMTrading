"""BrokerSync: the only component that talks to brokers about orders; reconciliation.

The engine never calls a broker (no network I/O under any lock): it marks an order's
`broker_status` (engine.models.BrokerState) and BrokerSync carries it out every
`interval_s`, each order in its own short transactions; every network call happens
with no transaction open.

Single writer (I5): `run` holds a Postgres advisory lock; a worker that cannot take it
skips its passes. `sending` orders are only reclaimed by the first pass after taking
the lock (startup recovery).

- `to_send` (never attempted): once the market is open, the local checks run again
  (locked/reduce-only portfolio, kill switch; not for liquidation orders). The order is
  claimed (`sending`, `broker_sent_at`, committed) and only then sent. A broker
  rejection rejects the order (PROVIDER_UNAVAILABLE + broker message) and notifies.
  Ruling R10: nothing falls back to the simulator on a linked portfolio: an order the
  adapter cannot take after `prepare`, or whose link is disabled, is rejected
  (notified); only an order whose portfolio the admin unlinked, never attempted, is
  released to the simulator.
- once a send was attempted (`broker_sent_at` set) the order is never expired,
  rejected or released before `find_order` (with every broker order id already linked
  on the link excluded, C1) gave an answer: found -> adopted (then canceled at the
  broker if it must not proceed: expired, link disabled/unlinked, kill switch, locked
  portfolio); not found -> for an idempotent adapter (Alpaca, by client_order_id) the
  answer is definitive and the order goes back to `to_send`; for Trading 212
  (Ruling R7) it is never resent and, after MATCH_WINDOW, rejected (or canceled when
  a cancel was requested) and left to reconciliation.
- live orders: `get_order`; a growth of filled_qty becomes a broker fill of the delta
  at `(avg_new * filled_new - avg_old * filled_old) / delta` (journal LIQUIDATION for
  liquidation orders). Without a broker price the fill waits; after
  FILL_PRICE_WAIT on a final order `broker.fill_price_missing` is notified once and
  it keeps waiting (no invented prices). Final states are applied. The daily loss
  check runs after the fill's commit, in its own transaction, on a memo prefetched
  before the portfolio lock.
- `cancel_requested`: cancel at the broker (an error is logged and retried, but the
  order is still read back so fills are never hidden, I2), apply the last fills and
  the final state; the order stays open until the broker confirms.
- `replace_requested` (modify): cancel; once canceled without fills it is sent again
  with a new client reference (broker_attempt + 1); if it filled, the fills stand.
- bracket exits at the broker are independent orders: when one fills, the cancel of
  its OCO sibling is requested and sent in the same pass.

`reconcile` compares the broker's positions with ours; it never corrects anything.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.base import BrokerAdapter, BrokerOrder
from smtrading.brokers.models import BrokerLink, Reconciliation
from smtrading.brokers.service import get_adapter
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import (
    NOTHING_TO_REDUCE,
    activate_linked,
    broker_send_check,
    execute_fill,
)
from smtrading.engine.models import (
    BROKER_LIQUIDATION_REASON,
    LIQUIDATION_ROLE,
    OPEN_STATUSES,
    BrokerState,
    Order,
    OrderStatus,
    OrderType,
)
from smtrading.engine.prefetch import prefetch_portfolio
from smtrading.engine.quotesource import MemoQuoteSource, QuoteSource
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
from smtrading.ledger.models import Account, AccountBalance, AccountKind, JournalType
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.daily_loss import check_daily_loss
from smtrading.risk.models import Notification

log = logging.getLogger("smtrading.brokers.sync")

SYNC_INTERVAL_S = 2.0
MATCH_WINDOW = timedelta(minutes=5)
FILL_PRICE_WAIT = timedelta(minutes=10)
ADVISORY_LOCK_KEY = 0x534D5442524B  # "SMTBRK"
UNSUPPORTED = "Ordine non supportato dal broker collegato"
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
    if price <= 0:
        log.warning(
            "order %s: delta price %s <= 0 (broker avg %s x %s, ours %s x %s); using the"
            " broker average", order.id, price, bo.avg_fill_price, bo.filled_qty,
            old_avg, before,
        )
        return bo.avg_fill_price
    return price


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


async def _linked_ids(session: AsyncSession, link_id: UUID, but: UUID) -> frozenset[str]:
    """Broker order ids already linked to other orders on `link_id` (C1)."""
    return frozenset(await session.scalars(
        select(Order.broker_order_id).where(
            Order.broker_link_id == link_id, Order.broker_order_id.is_not(None),
            Order.id != but,
        )
    ))


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
        self._followups: list[UUID] = []  # cancels to send in this pass
        self._loss_checks: set[UUID] = set()  # portfolios with a fill to check
        self._fill_qs: QuoteSource = qs  # the prefetched memo during an _update

    async def run(self) -> None:
        """Every interval_s a pass, while holding the advisory lock (single writer).
        The lock lives on a dedicated connection, checked before every pass: if that
        connection is lost (and the lock with it) the passes stop until it is retaken."""
        engine = self._sm.kw["bind"]
        warned = False
        while True:
            try:
                async with engine.connect() as conn:
                    try:
                        held = False
                        while True:
                            if not held:
                                held = bool(await conn.scalar(
                                    text("select pg_try_advisory_lock(:k)"),
                                    {"k": ADVISORY_LOCK_KEY},
                                ))
                                await conn.commit()
                                if held:
                                    log.info("broker sync: lock acquired")
                                    warned = False
                                    await self._pass(recover=True)
                                elif not warned:
                                    log.warning(
                                        "broker sync: another worker holds the lock; waiting"
                                    )
                                    warned = True
                            else:
                                await conn.scalar(text("select 1"))  # still holding it
                                await conn.commit()
                                await self._pass()
                            await asyncio.sleep(self._interval_s)
                    finally:
                        # never return a connection that may hold the lock to the pool
                        with contextlib.suppress(Exception):
                            await conn.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("broker sync: lock connection lost; reconnecting")
                await asyncio.sleep(self._interval_s)

    async def _pass(self, *, recover: bool = False) -> None:
        try:
            await self.tick(recover=recover)
        except Exception:
            log.exception("broker sync pass failed")

    async def tick(self, *, recover: bool = False) -> None:
        """One pass over the open broker-managed orders, oldest first. With `recover`
        (startup), `sending` orders left by a crash are reclaimed."""
        self._adapters = {}  # configs may change between passes
        async with self._sm() as session:
            rows = (await session.execute(
                select(Order.id, Order.broker_status, Order.broker_order_id,
                       Order.broker_sent_at)
                .where(Order.broker_link_id.is_not(None), Order.status.in_(_SYNCED))
                .order_by(Order.created_at, Order.id)
            )).all()
        for order_id, status, broker_order_id, sent_at in rows:
            try:
                await self._step(order_id, status, broker_order_id, sent_at, recover)
            except Exception:
                log.exception("broker sync of order %s failed", order_id)
            while self._followups:
                follow = self._followups.pop(0)
                try:
                    await self._cancel(follow)
                except Exception:
                    log.exception("broker cancel of order %s failed", follow)

    async def _step(self, order_id, status, broker_order_id, sent_at, recover) -> None:
        if status == BrokerState.SENDING:
            if recover:
                await self._recover_sending(order_id)
            return  # otherwise a send in flight
        if broker_order_id is not None:
            if status in (BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED):
                await self._cancel(order_id)
            else:
                await self._poll(order_id)
            return
        if sent_at is None and status == BrokerState.UNKNOWN:
            # a send attempted before broker_sent_at existed (pre-0011): attempted
            await self._update(order_id, self._backfill_sent_at)
            sent_at = True
        if sent_at is not None:
            await self._match(order_id)
        elif status == BrokerState.CANCEL_REQUESTED:  # never attempted: nothing at broker
            await self._update(order_id, self._cancel_unsent)
        else:
            await self._send(order_id)

    # -- helpers ---------------------------------------------------------------------

    async def _adapter(self, session: AsyncSession, link_id: UUID) -> BrokerAdapter:
        if link_id not in self._adapters:
            self._adapters[link_id] = await get_adapter(session, link_id, self._http)
        return self._adapters[link_id]

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()
        await dispatch_post_commit(session, self._redis)

    async def _update(self, order_id: UUID, fn, bo: BrokerOrder | None = None) -> None:
        """Run `fn(session, locked order)` in its own transaction (open orders only),
        then the daily loss checks of the portfolios it filled. When `bo` brings new
        fills, the quotes/FX the fill needs are prefetched before any lock (I1)."""
        async with self._sm() as session:
            try:
                self._fill_qs = await self._prefetch_fill(session, order_id, bo)
                order = await _lock(session, order_id)
                if order is None or order.status not in _SYNCED:
                    return
                await fn(session, order)
                await session.commit()
            except BaseException:
                await session.rollback()
                discard_post_commit(session)
                self._loss_checks.clear()
                self._followups.clear()  # requested by the rolled back work
                raise
            finally:
                self._fill_qs = self._qs
            await dispatch_post_commit(session, self._redis)
        await self._run_loss_checks()

    async def _prefetch_fill(
        self, session: AsyncSession, order_id: UUID, bo: BrokerOrder | None
    ) -> QuoteSource:
        """A memo with what a broker fill of `bo` needs, fetched with no lock held
        (the read transaction is ended before returning); `self._qs` if no fill."""
        if bo is None or bo.avg_fill_price is None:
            return self._qs
        row = (await session.execute(
            select(Order.portfolio_id, Order.symbol, Order.filled_qty)
            .where(Order.id == order_id)
        )).first()
        if row is None or bo.filled_qty <= (row.filled_qty or Decimal(0)):
            await session.commit()
            return self._qs
        memo = MemoQuoteSource.wrap(self._qs)
        await prefetch_portfolio(session, memo, row.portfolio_id, symbols=[row.symbol])
        await session.commit()
        return memo

    async def _run_loss_checks(self) -> None:
        """Daily loss check after a broker fill: own transaction, quotes prefetched
        before the portfolio lock (I1). Best-effort."""
        while self._loss_checks:
            portfolio_id = self._loss_checks.pop()
            try:
                async with self._sm() as session:
                    memo = MemoQuoteSource.wrap(self._qs)
                    await prefetch_portfolio(session, memo, portfolio_id, symbols=[])
                    portfolio = await lock_portfolio(session, portfolio_id)
                    await check_daily_loss(
                        session, memo, portfolio, self._clock(), redis=self._redis
                    )
                    await self._commit(session)
            except SMTError as e:
                log.warning("daily loss check of %s skipped: %s", portfolio_id, e.code)
            except Exception:
                log.exception("daily loss check of %s failed", portfolio_id)

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
        await self._closed(session, order)
        log.warning("order %s rejected: %s %s", order.id, code, message)

    def _set_final(self, session: AsyncSession, order: Order, status: OrderStatus) -> None:
        order.status = status
        order.updated_at = datetime.now(UTC)
        queue_event(session, f"order.{status}", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol, "by": "broker",
        })

    async def _closed(self, session: AsyncSession, order: Order) -> None:
        """After an order became final: untrack; unlock a portfolio whose broker
        liquidation is over (Ruling R10 b)."""
        await _untrack_if_idle(session, order.symbol)
        if order.role != LIQUIDATION_ROLE:
            return
        still = await session.scalar(
            select(Order.id).where(
                Order.portfolio_id == order.portfolio_id, Order.role == LIQUIDATION_ROLE,
                Order.status.in_(OPEN_STATUSES), Order.id != order.id,
            ).limit(1)
        )
        portfolio = await session.get(Portfolio, order.portfolio_id)
        if (
            still is None and portfolio.status == PortfolioStatus.locked
            and portfolio.reduce_only_reason == BROKER_LIQUIDATION_REASON
        ):
            portfolio.status = PortfolioStatus.active
            portfolio.reduce_only_reason = None
            log.warning("portfolio %s unlocked: broker liquidation done", portfolio.id)

    async def _refusal(
        self, session: AsyncSession, portfolio: Portfolio, order: Order, *, clamp: bool
    ) -> SMTError | None:
        """The fill-time local checks again, right before sending (Ruling R11; a
        liquidation skips the status checks only). A reduce_only order is clamped to the position
        (`clamp`); nothing to reduce is returned as an INVALID_ORDER error."""
        try:
            if not await broker_send_check(
                session, portfolio, order, clamp=clamp,
                status_checks=order.role != LIQUIDATION_ROLE,
            ):
                return SMTError(ErrorCode.INVALID_ORDER, NOTHING_TO_REDUCE)
        except SMTError as e:
            return e
        return None

    async def _must_stop(self, session: AsyncSession, order: Order) -> str | None:
        """Why an order found at the broker must not proceed, or None."""
        portfolio = await session.get(Portfolio, order.portfolio_id)
        link = await session.get(BrokerLink, order.broker_link_id)
        if order.expires_at is not None and order.expires_at <= self._clock():
            return "expired"
        if link is None or not link.enabled or portfolio.broker_link_id != link.id:
            return "broker link disabled or removed"
        refusal = await self._refusal(session, portfolio, order, clamp=False)
        return None if refusal is None else refusal.message

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

    async def _backfill_sent_at(self, session: AsyncSession, order: Order) -> None:
        if order.broker_sent_at is None and order.broker_order_id is None:
            order.broker_sent_at = order.broker_last_sync_at or order.updated_at

    async def _cancel_unsent(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is None and order.broker_sent_at is None:
            self._set_final(session, order, OrderStatus.canceled)
            await cancel_held_children(session, order, by="system")
            await self._closed(session, order)

    # -- send ------------------------------------------------------------------------

    async def _send(self, order_id: UUID, *, retry: bool = True) -> None:
        """Send a never-attempted order. A liquidation waits until the symbol's other
        orders at the broker are final (their cancels are sent and read back first, in
        this pass, so their fills are applied) and is then clamped to the position."""
        now = self._clock()
        async with self._sm() as session:
            order = await _lock(session, order_id)
            if (
                order is None or order.status not in _SYNCED
                or order.broker_status != BrokerState.TO_SEND
                or order.broker_sent_at is not None or order.broker_order_id is not None
            ):
                return
            portfolio = await session.get(Portfolio, order.portfolio_id)
            instrument = await session.get(Instrument, order.symbol)
            link = await session.get(BrokerLink, order.broker_link_id)
            if order.expires_at is not None and order.expires_at <= now:
                self._set_final(session, order, OrderStatus.expired)
                await cancel_held_children(session, order, by="system")
                await self._closed(session, order)
                await self._commit(session)
                return
            if portfolio.broker_link_id is None:  # the admin unlinked it: simulator
                self._release(order)
                await self._commit(session)
                return
            if link is None or not link.enabled or portfolio.broker_link_id != link.id:
                await self._reject(
                    session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                    "broker link disabled or changed",
                )
                await self._commit(session)
                return
            if not market_status(instrument.market, now).is_open:
                return
            if order.role == LIQUIDATION_ROLE:
                still_open, to_cancel = await self._cancel_others(session, order)
                if still_open:
                    await self._commit(session)
                    if retry:
                        await self._cancel_now(to_cancel)
                        await self._send(order_id, retry=False)
                    return
            refusal = await self._refusal(session, portfolio, order, clamp=True)
            if refusal is not None and refusal.message == NOTHING_TO_REDUCE:
                self._set_final(session, order, OrderStatus.canceled)
                order.reject_message = NOTHING_TO_REDUCE
                await cancel_held_children(session, order, by="system")
                await self._closed(session, order)
                await self._commit(session)
                return
            if refusal is not None:
                await self._reject(session, order, refusal.code, refusal.message)
                await self._commit(session)
                return
            adapter = await self._adapter(session, link.id)
            order.broker_status = BrokerState.SENDING
            order.broker_sent_at = now
            order.broker_last_sync_at = now
            await self._commit(session)  # claimed before any network I/O

        try:
            await adapter.prepare([instrument])
            supported = adapter.supports(order, instrument)
        except RuntimeError as e:  # nothing was sent: try again next pass
            log.warning("order %s: broker not ready: %s", order_id, e)
            await self._update(order_id, self._unclaim_unsent)
            return
        if not supported:
            await self._update(order_id, self._unsupported)
            return
        try:
            bo = await adapter.place_order(order, instrument)
        except RuntimeError as e:
            reason = str(e)
            log.warning("order %s: send failed: %s", order_id, reason)
            if adapter.idempotent_place:  # looked up by client reference next pass
                await self._update(order_id, self._attempted)
            else:
                await self._update(order_id, lambda s, o: self._mark_unknown(s, o, reason))
            return
        await self._update(order_id, lambda s, o: self._placed(s, o, bo), bo)

    async def _cancel_others(
        self, session: AsyncSession, order: Order
    ) -> tuple[list[UUID], list[UUID]]:
        """A broker liquidation: request the cancel of the portfolio's other open orders
        on the symbol at the broker (never-attempted ones are canceled locally).
        Returns (still open, those with a broker order to cancel right away)."""
        still_open, to_cancel = [], []
        for other in await session.scalars(
            select(Order).where(
                Order.portfolio_id == order.portfolio_id, Order.symbol == order.symbol,
                Order.id != order.id, Order.broker_link_id.is_not(None),
                Order.status.in_(_SYNCED),
            ).order_by(Order.created_at, Order.id).with_for_update()
        ):
            if (
                other.broker_order_id is None and other.broker_sent_at is None
                and other.broker_status != BrokerState.SENDING
            ):
                self._set_final(session, other, OrderStatus.canceled)
                await cancel_held_children(session, other, by="system")
                continue
            still_open.append(other.id)
            if other.broker_status != BrokerState.SENDING:
                other.broker_status = BrokerState.CANCEL_REQUESTED
                other.updated_at = datetime.now(UTC)
            if other.broker_order_id is not None:
                to_cancel.append(other.id)
        return still_open, to_cancel

    async def _cancel_now(self, order_ids: list[UUID]) -> None:
        for other in order_ids:
            try:
                await self._cancel(other)
            except Exception:
                log.exception("broker cancel of order %s before a liquidation failed", other)

    def _release(self, order: Order) -> None:
        """Back to the simulator: only for a portfolio the admin unlinked, never sent."""
        order.broker_link_id = None
        order.broker_status = None
        order.broker_replicated = False
        if order.type == OrderType.market and order.status == OrderStatus.pending:
            order.status = OrderStatus.queued  # executed by the processor's sweep
        order.updated_at = datetime.now(UTC)
        log.info("order %s released to the simulator", order.id)

    async def _unclaim_unsent(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.TO_SEND
            order.broker_sent_at = None
        elif order.broker_status == BrokerState.CANCEL_REQUESTED:
            order.broker_sent_at = None  # never reached the broker

    async def _attempted(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status == BrokerState.SENDING:
            order.broker_status = BrokerState.TO_SEND  # broker_sent_at stays: find first

    async def _unsupported(self, session: AsyncSession, order: Order) -> None:
        if order.broker_status not in (BrokerState.SENDING, BrokerState.CANCEL_REQUESTED):
            return
        order.broker_sent_at = None  # nothing was sent
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            await self._cancel_unsent(session, order)
            return
        order.broker_status = None
        await self._reject(session, order, ErrorCode.INVALID_ORDER, UNSUPPORTED)

    async def _placed(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if bo.status == "rejected":
            await self._reject(
                session, order, ErrorCode.PROVIDER_UNAVAILABLE,
                bo.message or "rejected by the broker",
            )
            return
        await self._adopt(session, order, bo)

    async def _adopt(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> bool:
        """Link `bo` to the order (refused if another order already has it, C1)."""
        taken = await session.scalar(
            select(Order.id).where(
                Order.broker_link_id == order.broker_link_id,
                Order.broker_order_id == bo.broker_order_id, Order.id != order.id,
            ).limit(1)
        )
        if taken is not None:
            log.error("order %s: broker order %s already belongs to order %s; not linked",
                      order.id, bo.broker_order_id, taken)
            return False
        order.broker_order_id = bo.broker_order_id
        order.broker_replicated = True
        if order.broker_status in (BrokerState.SENDING, BrokerState.TO_SEND,
                                   BrokerState.UNKNOWN):
            order.broker_status = bo.status
        queue_event(session, "order.broker_accepted", {
            "order_id": order.id, "portfolio_id": order.portfolio_id,
            "agent_id": order.agent_id, "symbol": order.symbol,
            "broker_order_id": bo.broker_order_id,
        })
        await self._apply(session, order, bo)
        return True

    # -- ambiguous sends (C2, Ruling R7) ---------------------------------------------

    async def _recover_sending(self, order_id: UUID) -> None:
        async with self._sm() as session:
            order = await _lock(session, order_id)
            if order is None or order.broker_status != BrokerState.SENDING:
                return
            adapter = await self._adapter(session, order.broker_link_id)
            if order.broker_sent_at is None:
                order.broker_sent_at = order.broker_last_sync_at or order.updated_at
            if adapter.idempotent_place:
                order.broker_status = BrokerState.TO_SEND
            else:
                await self._mark_unknown(session, order, "interrupted while sending")
            await self._commit(session)
        if adapter.idempotent_place:
            await self._match(order_id)

    async def _match(self, order_id: UUID) -> None:
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if (
                order is None or order.broker_link_id is None or order.status not in _SYNCED
                or order.broker_order_id is not None or order.broker_sent_at is None
            ):
                return
            instrument = await session.get(Instrument, order.symbol)
            adapter = await self._adapter(session, order.broker_link_id)
            exclude = await _linked_ids(session, order.broker_link_id, order.id)
            await session.commit()
        since = order.broker_sent_at
        bo = await adapter.find_order(order, instrument, since, exclude)
        if bo is not None:  # _adopt checks again, under the lock, that it is unused
            await self._update(order_id, lambda s, o: self._matched(s, o, bo), bo)
            return
        if adapter.idempotent_place:  # definitive: not at the broker
            await self._update(order_id, self._not_found)
        elif self._clock() - since > MATCH_WINDOW:
            await self._update(order_id, self._give_up)

    async def _matched(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id is not None or order.broker_sent_at is None:
            return
        if not await self._adopt(session, order, bo):
            return
        log.warning("order %s matched broker order %s", order.id, bo.broker_order_id)
        if order.status in _SYNCED:
            reason = (
                "cancel requested"
                if order.broker_status == BrokerState.CANCEL_REQUESTED
                else await self._must_stop(session, order)
            )
            if reason is not None:
                log.warning("order %s adopted but must not proceed (%s): canceling",
                            order.id, reason)
                order.broker_status = BrokerState.CANCEL_REQUESTED
                self._followups.append(order.id)

    async def _not_found(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is not None:
            return
        order.broker_sent_at = None
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            await self._cancel_unsent(session, order)
        else:
            order.broker_status = BrokerState.TO_SEND  # the next pass decides afresh

    async def _give_up(self, session: AsyncSession, order: Order) -> None:
        if order.broker_order_id is not None:
            return
        if order.broker_status == BrokerState.CANCEL_REQUESTED:
            self._set_final(session, order, OrderStatus.canceled)
            await cancel_held_children(session, order, by="system")
            await self._closed(session, order)
            return
        await self._reject(
            session, order, ErrorCode.PROVIDER_UNAVAILABLE,
            "broker outcome unknown: no matching broker order found; check the reconciliation",
        )

    # -- live orders -----------------------------------------------------------------

    async def _read(self, order_id: UUID) -> tuple[Order, BrokerAdapter] | None:
        """The order and its adapter, read without locks (no transaction is kept open
        across the network call that follows)."""
        async with self._sm() as session:
            order = await session.get(Order, order_id)
            if (
                order is None or order.broker_link_id is None or order.status not in _SYNCED
                or order.broker_order_id is None
            ):
                return None
            adapter = await self._adapter(session, order.broker_link_id)
            await session.commit()
        return order, adapter

    async def _poll(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, adapter = read
        bo = await adapter.get_order(order.broker_order_id)
        await self._update(order_id, lambda s, o: self._apply_same(s, o, bo), bo)

    async def _cancel(self, order_id: UUID) -> None:
        read = await self._read(order_id)
        if read is None:
            return
        order, adapter = read
        try:
            await adapter.cancel_order(order.broker_order_id)
        except RuntimeError as e:  # read it back anyway: fills must not be hidden (I2)
            log.warning("order %s: broker cancel failed (%s); retried next pass", order_id, e)
        bo = await adapter.get_order(order.broker_order_id)
        await self._update(order_id, lambda s, o: self._apply_same(s, o, bo), bo)

    async def _apply_same(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        if order.broker_order_id == bo.broker_order_id:  # not resubmitted meanwhile
            await self._apply(session, order, bo)

    async def _price_missing(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """A final order with fills but no broker price: notify once after
        FILL_PRICE_WAIT; keep waiting (manual reconciliation, no invented price)."""
        if bo.status not in _FINAL and bo.status != "filled":
            return
        since = order.broker_last_sync_at or order.updated_at
        if self._clock() - since <= FILL_PRICE_WAIT:
            return
        sent = await session.scalar(
            select(Notification.id).where(
                Notification.portfolio_id == order.portfolio_id,
                Notification.kind == "broker.fill_price_missing",
                Notification.payload["order_id"].astext == str(order.id),
            ).limit(1)
        )
        if sent is None:
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="broker.fill_price_missing",
                payload={"order_id": order.id, "symbol": order.symbol,
                         "broker_filled_qty": bo.filled_qty, "filled_qty": order.filled_qty},
            )
            log.error("order %s: broker fill price missing for %s; waiting",
                      order.id, bo.filled_qty - order.filled_qty)

    async def _apply(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        """Apply the broker's view of the order (caller holds the locks)."""
        delta = bo.filled_qty - (order.filled_qty or Decimal(0))
        if delta > 0:
            if bo.avg_fill_price is None:
                await self._price_missing(session, order, bo)
                return  # no broker price yet: wait for it
            price = delta_price(order, bo, delta)
            await execute_fill(
                session, self._fill_qs, order.id, fill_qty=delta, fill_price=price,
                source="broker", final=bo.status == "filled", redis=self._redis,
                journal_type=(
                    JournalType.LIQUIDATION if order.role == LIQUIDATION_ROLE
                    else JournalType.TRADE
                ),
            )
            self._loss_checks.add(order.portfolio_id)
        order.broker_last_sync_at = self._clock()
        if order.status not in _SYNCED:  # filled by execute_fill
            order.broker_status = bo.status
            await self._after_fill(session, order)
            return
        if bo.status == "filled":  # e.g. a notional order, or the broker filled less
            order.status = OrderStatus.filled
            order.broker_status = bo.status
            order.updated_at = datetime.now(UTC)
            await activate_linked(session, order)
            await self._after_fill(session, order)
            return
        if bo.status in _FINAL:
            await self._finish(session, order, bo)
            return
        if order.broker_status not in (
            BrokerState.CANCEL_REQUESTED, BrokerState.REPLACE_REQUESTED
        ):
            order.broker_status = bo.status

    async def _after_fill(self, session: AsyncSession, order: Order) -> None:
        """A filled order: its OCO siblings at the broker are canceled in this pass."""
        if order.oco_group_id is not None:
            await session.flush()
            self._followups += list(await session.scalars(
                select(Order.id).where(
                    Order.oco_group_id == order.oco_group_id, Order.id != order.id,
                    Order.broker_status == BrokerState.CANCEL_REQUESTED,
                    Order.status.in_(_SYNCED),
                )
            ))
        await self._closed(session, order)

    async def _finish(self, session: AsyncSession, order: Order, bo: BrokerOrder) -> None:
        replacing = order.broker_status == BrokerState.REPLACE_REQUESTED
        if replacing and bo.status == "canceled" and order.filled_qty == 0:
            order.broker_order_id = None
            order.broker_replicated = False
            order.broker_sent_at = None
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
        await self._after_fill(session, order)


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


async def reconcile(
    sessionmaker: async_sessionmaker[AsyncSession], link_id: UUID, http
) -> list[Reconciliation]:
    """Compare the broker's positions with ours and store (committed) one
    Reconciliation per linked portfolio (diffs: [{symbol, ours, broker}], quantities as
    strings). Portfolios sharing a link share the broker account, so `ours` is their
    sum. Nothing is corrected. Reads, broker calls and writes use separate sessions:
    no transaction is open during network I/O."""
    async with sessionmaker() as session:
        link = await session.get(BrokerLink, link_id)
        if link is None:
            raise SMTError(ErrorCode.NOT_FOUND, f"broker link {link_id} not found")
        portfolio_ids = list(await session.scalars(
            select(Portfolio.id).where(Portfolio.broker_link_id == link_id)
            .order_by(Portfolio.id)
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
    async with sessionmaker() as session:
        session.add_all(rows)
        await session.commit()
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
        try:
            rows = await reconcile(sessionmaker, link_id, http)
            status, error = "ok", None
        except Exception as e:  # noqa: BLE001  (any failure goes to the link status)
            log.warning("reconciliation of broker link %s failed: %s", link_id, e)
            rows, status, error = [], "error", str(e)[:500]
        async with sessionmaker() as session:
            link = await session.get(BrokerLink, link_id)
            if link is not None:
                link.status, link.last_error = status, error
                await session.commit()
        bad = [r for r in rows if not r.ok]
        if bad:
            mismatched += len(bad)
            log.warning("broker link %s: positions differ: %s", link_id, bad[0].diffs)
    return mismatched
