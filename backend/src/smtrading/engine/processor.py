"""OrderProcessor: the worker loop that executes resting orders.

- `on_tick` evaluates the open orders of one symbol against a streamed quote (throttled
  per symbol to `min_eval_interval_s`).
- `sweep` (every `sweep_every_s`) expires DAY orders, executes `queued` market orders once
  their market is open and evaluates `pending`/`triggered` orders with the current quotes:
  the fallback for symbols without a stream, and the catch-up after a restart (a stop
  crossed while the worker was down fills at the current, worse, price).

Closed markets (Ruling R14): nothing is evaluated while `market_status(...).is_open` is
false. A quote older than the asset class stale threshold is never acted on; a queued
market order still without a fresh quote 10 minutes after the open is rejected with
STALE_QUOTE.

Each order runs in its own session and transaction. Evaluation first reads the order
without locks; when there is something to do, the portfolio row and then the order row
are locked FOR UPDATE (Ruling R11) and the decision is taken again on the locked row,
so concurrent processors produce at most one fill. Redis effects are dispatched after
commit (Ruling R10). An SMTError while executing rejects the order with its code (new
transaction) and notifies the portfolio; any other error is logged, rolled back and the
order retried on a later pass.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import OPEN_STATUSES
from smtrading.engine.triggers import OrderView, TriggerAction, TriggerResult, evaluate
from smtrading.events import (
    discard_post_commit,
    dispatch_post_commit,
    notify,
    queue_event,
    queue_untrack,
)
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.marketdata.serialization import loads_quote
from smtrading.marketdata.types import Quote
from smtrading.portfolios.models import Portfolio
from smtrading.risk.settings import get_default_spread_bps, get_stale_threshold

log = logging.getLogger("smtrading.engine.processor")

TICKS_PATTERN = "ticks:*"
STALE_REJECT_AFTER_OPEN = timedelta(minutes=10)
_ACTIVE = (OrderStatus.queued, OrderStatus.pending, OrderStatus.triggered)
_MARKET_ONCE_TRIGGERED = (OrderType.stop, OrderType.trailing_stop)


@dataclass(frozen=True)
class _Candidate:
    id: UUID
    symbol: str
    market: str


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def _decide(order: Order, bid, ask) -> TriggerResult:
    if order.status == OrderStatus.triggered and order.type in _MARKET_ONCE_TRIGGERED:
        return TriggerResult(TriggerAction.fill)  # a fired stop is a market order
    if (
        order.type == OrderType.trailing_stop and order.status == OrderStatus.pending
        and order.trail_ref_price is None
    ):
        # placed without a quote: the first evaluation only sets the reference
        ref = bid if order.side == OrderSide.sell else ask
        return TriggerResult(TriggerAction.update_trail, ref)
    return evaluate(
        OrderView(
            type=order.type, side=order.side, status=order.status,
            limit_price=order.limit_price, stop_price=order.stop_price,
            trail_pct=order.trail_pct, trail_amount=order.trail_amount,
            trail_ref_price=order.trail_ref_price,
        ),
        bid, ask,
    )


async def _lock_order(session: AsyncSession, order_id: UUID) -> Order | None:
    """Portfolio row first, then the order row (Ruling R11)."""
    portfolio_id = await session.scalar(select(Order.portfolio_id).where(Order.id == order_id))
    if portfolio_id is None:
        return None
    await session.execute(
        select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update()
    )
    return (
        await session.execute(
            select(Order).where(Order.id == order_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _untrack_if_idle(session: AsyncSession, symbol: str) -> None:
    await session.flush()
    still_open = await session.scalar(
        select(Order.id)
        .where(Order.symbol == symbol, Order.status.in_(OPEN_STATUSES))
        .limit(1)
    )
    if still_open is None:
        queue_untrack(session, symbol, "orders")


class OrderProcessor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis,
        qs: QuoteSource,
        *,
        sweep_every_s: float = 1.0,
        min_eval_interval_s: float = 0.25,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._qs = qs
        self._sweep_every_s = sweep_every_s
        self._min_eval_interval_s = min_eval_interval_s
        self._clock = clock
        self._last_eval: dict[str, datetime] = {}

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._listen())
            tg.create_task(self._sweep_loop())

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.psubscribe(TICKS_PATTERN)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                try:
                    symbol = _s(message["channel"]).split(":", 1)[1]
                    await self.on_tick(symbol, loads_quote(_s(message["data"])))
                except Exception:
                    log.exception("tick processing failed: %r", message.get("channel"))
        finally:
            await pubsub.aclose()

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("order sweep failed")
            await asyncio.sleep(self._sweep_every_s)

    async def on_tick(self, symbol: str, quote: Quote) -> int:
        """Evaluate `symbol`'s open orders at `quote`; number executed or triggered."""
        now = self._clock()
        last = self._last_eval.get(symbol)
        if last is not None and (now - last).total_seconds() < self._min_eval_interval_s:
            return 0
        self._last_eval[symbol] = now
        candidates = await self._candidates(symbol)
        return await self._process_all(candidates, {symbol: quote}, now)

    async def sweep(self) -> None:
        now = self._clock()
        await self._expire(now)
        candidates = await self._candidates()
        opened = self._open_markets({c.market for c in candidates}, now)
        candidates = [c for c in candidates if c.market in opened]
        quotes = await self._quotes(sorted({c.symbol for c in candidates}))
        await self._process_all(candidates, quotes, now)

    # -- selection -------------------------------------------------------------------

    async def _candidates(self, symbol: str | None = None) -> list[_Candidate]:
        stmt = (
            select(Order.id, Order.symbol, Instrument.market)
            .join(Instrument, Instrument.symbol == Order.symbol)
            .where(Order.status.in_(_ACTIVE))
            .order_by(Order.created_at, Order.id)
        )
        if symbol is not None:
            stmt = stmt.where(Order.symbol == symbol)
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [_Candidate(*row) for row in rows]

    @staticmethod
    def _open_markets(markets: set[str], now: datetime) -> set[str]:
        return {m for m in markets if market_status(m, now).is_open}

    async def _quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        try:
            return await self._qs.get_quotes(symbols)
        except SMTError:
            pass
        quotes: dict[str, Quote] = {}
        for symbol in symbols:  # all-or-error: retry one by one
            try:
                quotes.update(await self._qs.get_quotes([symbol]))
            except SMTError as e:
                log.warning("no quote for %s: %s", symbol, e.code)
        return quotes

    async def _process_all(
        self, candidates: list[_Candidate], quotes: dict[str, Quote], now: datetime
    ) -> int:
        opened = self._open_markets({c.market for c in candidates}, now)
        done = 0
        for c in candidates:
            quote = quotes.get(c.symbol)
            if c.market not in opened or quote is None:
                continue
            try:
                if await self._process(c, quote, now):
                    done += 1
            except Exception:
                log.exception("processing order %s failed", c.id)
        return done

    # -- one order -------------------------------------------------------------------

    async def _process(self, c: _Candidate, quote: Quote, now: datetime) -> bool:
        async with self._sm() as session:
            try:
                acted = await self._evaluate(session, c, quote, now)
                await session.commit()
            except SMTError as e:
                await session.rollback()
                discard_post_commit(session)
                await self._reject(c.id, e)
                return False
            except BaseException:
                await session.rollback()
                discard_post_commit(session)
                raise
            await dispatch_post_commit(session, self._redis)
            return acted

    async def _evaluate(
        self, session: AsyncSession, c: _Candidate, quote: Quote, now: datetime
    ) -> bool:
        """True when the order was executed or triggered."""
        order = await session.get(Order, c.id)
        if order is None or order.status not in _ACTIVE:
            return False
        instrument = await session.get(Instrument, order.symbol)
        threshold = await get_stale_threshold(session, instrument.asset_class)
        if (now - quote.ts).total_seconds() > threshold:
            if order.status == OrderStatus.queued and market_status(
                c.market, now - STALE_REJECT_AFTER_OPEN
            ).is_open:
                raise SMTError(
                    ErrorCode.STALE_QUOTE,
                    f"no quote newer than {threshold}s since the market opened",
                    {"symbol": order.symbol},
                )
            return False  # wait for a fresh quote
        spread = await get_default_spread_bps(session, instrument.asset_class)
        bid, ask = effective_bid_ask(QuoteView(quote.bid, quote.ask, quote.last, quote.ts), spread)
        if _decide(order, bid, ask).action == TriggerAction.none:
            return False

        order = await _lock_order(session, c.id)
        if order is None or order.status not in _ACTIVE:
            return False
        result = _decide(order, bid, ask)  # again, on the locked row
        if result.new_trail_ref is not None:
            order.trail_ref_price = result.new_trail_ref
        if result.action in (TriggerAction.none, TriggerAction.update_trail):
            if result.action == TriggerAction.update_trail:
                order.updated_at = datetime.now(UTC)
            return False
        if result.action == TriggerAction.trigger_to_limit:
            order.status = OrderStatus.triggered
            order.updated_at = datetime.now(UTC)
            return True
        if result.action == TriggerAction.trigger_to_market:
            order.status = OrderStatus.triggered
            await session.flush()
        filled = await execute_fill(session, self._qs, order.id, quote=quote)
        if filled is None:
            return False
        await _untrack_if_idle(session, order.symbol)
        return True

    async def _reject(self, order_id: UUID, error: SMTError) -> None:
        async with self._sm() as session:
            order = await _lock_order(session, order_id)
            if order is None or order.status not in _ACTIVE:
                return
            order.status = OrderStatus.rejected
            order.reject_code = str(error.code)
            order.reject_message = (error.message or "")[:512]
            order.updated_at = datetime.now(UTC)
            await notify(
                session, portfolio_id=order.portfolio_id, agent_id=order.agent_id,
                kind="order.rejected",
                payload={
                    "order_id": order.id, "symbol": order.symbol, "code": str(error.code),
                    "message": error.message,
                },
            )
            await _untrack_if_idle(session, order.symbol)
            await session.commit()
            await dispatch_post_commit(session, self._redis)
        log.warning("order %s rejected: %s %s", order_id, error.code, error.message)

    # -- expiry ----------------------------------------------------------------------

    async def _expire(self, now: datetime) -> None:
        async with self._sm() as session:
            ids = list(await session.scalars(
                select(Order.id)
                .where(Order.status.in_(OPEN_STATUSES), Order.expires_at <= now)
                .order_by(Order.expires_at, Order.id)
            ))
        for order_id in ids:
            try:
                await self._expire_one(order_id, now)
            except Exception:
                log.exception("expiring order %s failed", order_id)

    async def _expire_one(self, order_id: UUID, now: datetime) -> None:
        async with self._sm() as session:
            order = await _lock_order(session, order_id)
            if (
                order is None or order.status not in OPEN_STATUSES
                or order.expires_at is None or order.expires_at > now
            ):
                return
            order.status = OrderStatus.expired
            order.updated_at = datetime.now(UTC)
            queue_event(session, "order.expired", {
                "order_id": order.id, "portfolio_id": order.portfolio_id,
                "agent_id": order.agent_id, "symbol": order.symbol,
            })
            await _untrack_if_idle(session, order.symbol)
            await session.commit()
            await dispatch_post_commit(session, self._redis)

