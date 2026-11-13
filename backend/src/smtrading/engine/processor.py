"""OrderProcessor: the worker loop that executes resting orders.

- Ticks (`ticks:*` pub/sub) are coalesced: the listener only keeps the latest quote per
  symbol and marks it dirty when the symbol has active orders (an in-memory set refreshed
  by each sweep and after each acting pass). A drain task evaluates dirty symbols with
  their latest quote, at most once per `min_eval_interval_s` per symbol (`on_tick`).
- `sweep` (every `sweep_every_s`) expires DAY orders, executes `queued` market orders once
  their market is open and evaluates `pending`/`triggered` orders with the current quotes:
  the fallback for symbols without a stream, and the catch-up after a restart (a stop
  crossed while the worker was down fills at the current, worse, price).

Closed markets (Ruling R14): nothing is evaluated while `market_status(...).is_open` is
false. A quote older than the asset class stale threshold is never acted on. Ruling R15:
a queued market order still without a fresh quote (stale or missing) 10 minutes after
the open is rejected with STALE_QUOTE. Orders past `expires_at` are left to the expiry.

Each order runs in its own session and transaction. Evaluation first reads the order
without locks; when there is something to do, the portfolio row and then the order row
are locked FOR UPDATE (Ruling R11) and the decision is taken again on the locked row,
so concurrent processors produce at most one fill. Redis effects are dispatched after
commit (Ruling R10).

Margin (Rulings R8, R10): ticks are also kept for symbols held by margin portfolios
(margin loan or short; the map is refreshed by each sweep). After a symbol's orders are
evaluated, each margin portfolio holding it is checked with `check_portfolio_margin` in
its own transaction, at most once every `margin_check_every_s` per portfolio. After a
fill, the daily loss limit of the portfolio is checked in a new transaction. Both are
best-effort: errors are logged and never stop order processing.

Errors: a business SMTError (`REJECTING_CODES`) rejects the order with its code in a new
transaction, cancels its held bracket children (Ruling R17) and notifies the portfolio.
Any other error (provider outage, rate limit, bugs) is rolled back and logged; the order
is left as it was and retried on a later pass.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.execution import execute_fill
from smtrading.engine.models import Order, OrderSide, OrderStatus, OrderType
from smtrading.engine.pricing import QuoteView, effective_bid_ask
from smtrading.engine.quotesource import QuoteSource
from smtrading.engine.service import OPEN_STATUSES, cancel_held_children
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
from smtrading.risk.daily_loss import check_daily_loss
from smtrading.risk.margin import check_margin_and_commit, margin_holdings
from smtrading.risk.settings import get_default_spread_bps, get_stale_threshold

log = logging.getLogger("smtrading.engine.processor")

TICKS_PATTERN = "ticks:*"
STALE_REJECT_AFTER_OPEN = timedelta(minutes=10)
FAILING_SYMBOL_BACKOFF = timedelta(seconds=60)
REJECTING_CODES = frozenset({
    ErrorCode.INSUFFICIENT_BUYING_POWER, ErrorCode.SHORT_NOT_ALLOWED,
    ErrorCode.SYMBOL_NOT_FOUND, ErrorCode.INVALID_ORDER, ErrorCode.REDUCE_ONLY,
    ErrorCode.KILL_SWITCH_ACTIVE, ErrorCode.PORTFOLIO_LOCKED, ErrorCode.RISK_LIMIT_EXCEEDED,
    ErrorCode.STALE_QUOTE,
})
_ACTIVE = (OrderStatus.queued, OrderStatus.pending, OrderStatus.triggered)
_MARKET_ONCE_TRIGGERED = (OrderType.stop, OrderType.trailing_stop)


@dataclass(frozen=True)
class _Candidate:
    id: UUID
    symbol: str
    status: OrderStatus
    type: OrderType
    market: str


@dataclass
class _Pass:
    """State shared by the orders of one on_tick/sweep pass."""

    now: datetime
    open_markets: dict[str, bool] = field(default_factory=dict)
    settings: dict[str, tuple[int, int]] = field(default_factory=dict)  # stale_s, spread

    def is_open(self, market: str) -> bool:
        if market not in self.open_markets:
            self.open_markets[market] = market_status(market, self.now).is_open
        return self.open_markets[market]

    def open_long_enough(self, market: str) -> bool:
        """Open now and already open STALE_REJECT_AFTER_OPEN ago (Ruling R15)."""
        return self.is_open(market) and market_status(
            market, self.now - STALE_REJECT_AFTER_OPEN
        ).is_open


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def _stale_error(symbol: str, detail: str) -> SMTError:
    return SMTError(ErrorCode.STALE_QUOTE, detail, {"symbol": symbol})


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


def _actionable(order: Order | None, now: datetime) -> bool:
    return (
        order is not None and order.status in _ACTIVE
        and (order.expires_at is None or order.expires_at > now)
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
        margin_check_every_s: float = 2.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._qs = qs
        self._sweep_every_s = sweep_every_s
        self._min_eval_interval_s = min_eval_interval_s
        self._clock = clock
        self._last_eval: dict[str, datetime] = {}
        self._active: set[str] = set()  # symbols with queued/pending/triggered orders
        self._latest: dict[str, Quote] = {}
        self._dirty: set[str] = set()
        self._wake = asyncio.Event()
        self._failing: dict[str, datetime] = {}  # symbol -> quote fetch failed at
        self._margin_check_every_s = margin_check_every_s
        self._margin: dict[str, set[UUID]] = {}  # symbol -> margin portfolios holding it
        self._margin_checked: dict[UUID, datetime] = {}

    # -- loops -----------------------------------------------------------------------

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._listen())
            tg.create_task(self._drain_loop())
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
                    self.submit_tick(symbol, loads_quote(_s(message["data"])))
                except Exception:
                    log.exception("bad tick message on %r", message.get("channel"))
        finally:
            await pubsub.aclose()

    def submit_tick(self, symbol: str, quote: Quote) -> None:
        """Keep the latest quote; mark the symbol for evaluation if it has active orders
        or is held by a margin portfolio."""
        if symbol not in self._active and symbol not in self._margin:
            return
        self._latest[symbol] = quote
        self._dirty.add(symbol)
        self._wake.set()

    async def drain(self) -> float | None:
        """Evaluate the dirty symbols that are not throttled, each with its latest quote.

        Returns the seconds until a throttled dirty symbol becomes due, or None.
        """
        now = self._clock()
        wait: float | None = None
        for symbol in sorted(self._dirty):
            remaining = self._throttle_remaining(symbol, now)
            if remaining > 0:
                wait = remaining if wait is None else min(wait, remaining)
                continue
            self._dirty.discard(symbol)
            quote = self._latest.pop(symbol)
            if symbol in self._active:
                try:
                    await self.on_tick(symbol, quote)
                except Exception:
                    log.exception("tick evaluation failed for %s", symbol)
            else:
                self._last_eval[symbol] = now
            await self._check_margin(symbol)
        return wait

    async def _drain_loop(self) -> None:
        while True:
            self._wake.clear()
            wait = await self.drain()
            if self._dirty and wait is not None:
                try:
                    await asyncio.wait_for(self._wake.wait(), wait)
                except TimeoutError:
                    pass
            elif not self._dirty:
                await self._wake.wait()

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("order sweep failed")
            await asyncio.sleep(self._sweep_every_s)

    # -- passes ----------------------------------------------------------------------

    def _throttle_remaining(self, symbol: str, now: datetime) -> float:
        last = self._last_eval.get(symbol)
        if last is None:
            return 0.0
        return self._min_eval_interval_s - (now - last).total_seconds()

    async def on_tick(self, symbol: str, quote: Quote) -> int:
        """Evaluate `symbol`'s open orders at `quote`; number executed or triggered."""
        now = self._clock()
        if self._throttle_remaining(symbol, now) > 0:
            return 0
        self._last_eval[symbol] = now
        candidates = await self._candidates(symbol)
        done = await self._process_all(candidates, {symbol: quote}, _Pass(now))
        if done:
            await self._refresh_active()
        return done

    async def sweep(self) -> None:
        ctx = _Pass(self._clock())
        await self._expire(ctx.now)
        await self._refresh_margin()
        candidates = await self._candidates()
        self._active = {c.symbol for c in candidates}
        candidates = [c for c in candidates if ctx.is_open(c.market)]
        quotes = await self._quotes(sorted({c.symbol for c in candidates}), ctx.now)
        if await self._process_all(candidates, quotes, ctx):
            await self._refresh_active()

    # -- selection -------------------------------------------------------------------

    async def _candidates(self, symbol: str | None = None) -> list[_Candidate]:
        stmt = (
            select(Order.id, Order.symbol, Order.status, Order.type, Instrument.market)
            .join(Instrument, Instrument.symbol == Order.symbol)
            .where(Order.status.in_(_ACTIVE))
            .order_by(Order.created_at, Order.id)
        )
        if symbol is not None:
            stmt = stmt.where(Order.symbol == symbol)
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [_Candidate(*row) for row in rows]

    async def _refresh_active(self) -> None:
        async with self._sm() as session:
            self._active = set(await session.scalars(
                select(Order.symbol).where(Order.status.in_(_ACTIVE)).distinct()
            ))

    async def _refresh_margin(self) -> None:
        try:
            async with self._sm() as session:
                self._margin = await margin_holdings(session)
        except Exception:
            log.exception("refreshing margin holdings failed")

    async def _check_margin(self, symbol: str) -> None:
        """Margin check of the margin portfolios holding `symbol` (throttled, logged)."""
        for portfolio_id in sorted(self._margin.get(symbol, ())):
            now = self._clock()
            last = self._margin_checked.get(portfolio_id)
            if last is not None and (now - last).total_seconds() < self._margin_check_every_s:
                continue
            self._margin_checked[portfolio_id] = now
            try:
                result = await check_margin_and_commit(
                    self._sm, self._qs, self._redis, portfolio_id, now=now
                )
            except Exception:
                log.exception("margin check of portfolio %s failed", portfolio_id)
                continue
            if result == "liquidated":
                await self._refresh_margin()
                await self._refresh_active()

    async def _after_fill(self, order_id: UUID) -> None:
        """Daily loss check of the portfolio of a just filled order (best-effort)."""
        try:
            async with self._sm() as session:
                order = await session.get(Order, order_id)
                if order is None or order.status != OrderStatus.filled:
                    return
                portfolio = (
                    await session.execute(
                        select(Portfolio).where(Portfolio.id == order.portfolio_id)
                        .with_for_update()
                    )
                ).scalar_one()
                await check_daily_loss(session, self._qs, portfolio, self._clock())
                await session.commit()
        except SMTError as e:
            log.warning("daily loss check after order %s skipped: %s", order_id, e.code)
        except Exception:
            log.exception("daily loss check after order %s failed", order_id)

    async def _quotes(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        """Quotes for `symbols`; a symbol whose fetch failed is skipped for 60s."""
        symbols = [
            s for s in symbols
            if s not in self._failing or now - self._failing[s] >= FAILING_SYMBOL_BACKOFF
        ]
        if not symbols:
            return {}
        try:
            quotes = await self._qs.get_quotes(symbols)
            for s in symbols:
                self._failing.pop(s, None)
            return quotes
        except SMTError:
            pass
        quotes: dict[str, Quote] = {}
        for symbol in symbols:  # all-or-error: retry one by one
            try:
                quotes.update(await self._qs.get_quotes([symbol]))
                self._failing.pop(symbol, None)
            except SMTError as e:
                self._failing[symbol] = now
                log.warning("no quote for %s: %s", symbol, e.code)
        return quotes

    async def _process_all(
        self, candidates: list[_Candidate], quotes: dict[str, Quote], ctx: _Pass
    ) -> int:
        done = 0
        for c in candidates:
            if not ctx.is_open(c.market):
                continue
            quote = quotes.get(c.symbol)
            try:
                if quote is None:
                    if (
                        c.status == OrderStatus.queued and c.type == OrderType.market
                        and ctx.open_long_enough(c.market)
                    ):
                        await self._reject(
                            c.id, _stale_error(c.symbol, "no quote since the market opened")
                        )
                    continue
                if await self._process(c, quote, ctx):
                    done += 1
            except Exception:
                log.exception("processing order %s failed", c.id)
        return done

    # -- one order -------------------------------------------------------------------

    async def _process(self, c: _Candidate, quote: Quote, ctx: _Pass) -> bool:
        async with self._sm() as session:
            try:
                acted = await self._evaluate(session, c, quote, ctx)
                await session.commit()
            except BaseException as e:
                await session.rollback()
                discard_post_commit(session)
                if isinstance(e, SMTError) and e.code in REJECTING_CODES:
                    await self._reject(c.id, e)
                    return False
                raise
            await dispatch_post_commit(session, self._redis)
        if acted:
            await self._after_fill(c.id)
        return acted

    async def _settings(
        self, session: AsyncSession, ctx: _Pass, asset_class: str
    ) -> tuple[int, int]:
        if asset_class not in ctx.settings:
            ctx.settings[asset_class] = (
                await get_stale_threshold(session, asset_class),
                await get_default_spread_bps(session, asset_class),
            )
        return ctx.settings[asset_class]

    async def _evaluate(
        self, session: AsyncSession, c: _Candidate, quote: Quote, ctx: _Pass
    ) -> bool:
        """True when the order was executed or triggered."""
        now = ctx.now
        order = await session.get(Order, c.id)
        if not _actionable(order, now):
            return False
        instrument = await session.get(Instrument, order.symbol)
        threshold, spread = await self._settings(session, ctx, instrument.asset_class)
        if (now - quote.ts).total_seconds() > threshold:
            if order.status == OrderStatus.queued and ctx.open_long_enough(c.market):
                raise _stale_error(
                    order.symbol, f"no quote newer than {threshold}s since the market opened"
                )
            return False  # wait for a fresh quote
        bid, ask = effective_bid_ask(QuoteView(quote.bid, quote.ask, quote.last, quote.ts), spread)
        if _decide(order, bid, ask).action == TriggerAction.none:
            return False

        order = await _lock_order(session, c.id)
        if not _actionable(order, now):
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
            await cancel_held_children(session, order, by="system")
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
            await cancel_held_children(session, order, by="system")
            await _untrack_if_idle(session, order.symbol)
            await session.commit()
            await dispatch_post_commit(session, self._redis)
