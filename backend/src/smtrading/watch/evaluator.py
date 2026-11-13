"""AlertEvaluator: the worker loop that fires price alerts.

- Ticks (`ticks:*` pub/sub) are coalesced as in the OrderProcessor: the listener keeps
  only the latest quote per symbol, and only for symbols with active alerts (an in-memory
  set refreshed by each sweep and after each trigger). A drain task evaluates the dirty
  symbols with their latest quote, at most once per `min_eval_interval_s` per symbol.
- `sweep` (every `sweep_every_s`, default 5s) evaluates every symbol with active alerts
  at `qs.get_quotes`: the fallback for symbols without a stream and the catch-up after a
  restart.

Price: `last`, else the bid/ask mid. `price_above`/`price_below` compare it with `value`
(>= / <=); `change_pct_above`/`change_pct_below` compare (price - ref_price) / ref_price
* 100 with `value` (>= / <=).

Each trigger runs in its own transaction: the alert row is locked with FOR UPDATE SKIP
LOCKED and must still be active, so concurrent evaluators fire an alert once. It becomes
`triggered` (triggered_at, triggered_price), a `price_alert` notification addressed to
the agent is written, and the symbol is untracked (source "alerts") once it has no active
alert left. Errors are logged; the alert stays active and is retried on a later pass.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.quotesource import QuoteSource
from smtrading.events import discard_post_commit, dispatch_post_commit, notify
from smtrading.marketdata.serialization import loads_quote
from smtrading.marketdata.types import Quote
from smtrading.watch.models import Alert, AlertCondition
from smtrading.watch.service import ACTIVE, TRIGGERED, reference_price, untrack_if_no_active_alert

log = logging.getLogger("smtrading.watch.evaluator")

TICKS_PATTERN = "ticks:*"


@dataclass(frozen=True)
class _Candidate:
    id: UUID
    condition: AlertCondition
    value: Decimal
    ref_price: Decimal | None


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def is_hit(condition: AlertCondition, value: Decimal, ref_price: Decimal | None,
           price: Decimal) -> bool:
    if condition == AlertCondition.price_above:
        return price >= value
    if condition == AlertCondition.price_below:
        return price <= value
    if ref_price is None or ref_price <= 0:
        return False
    change_pct = (price - ref_price) / ref_price * 100
    if condition == AlertCondition.change_pct_above:
        return change_pct >= value
    return change_pct <= value


class AlertEvaluator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis,
        qs: QuoteSource,
        *,
        sweep_every_s: float = 5.0,
        min_eval_interval_s: float = 0.25,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._qs = qs
        self._sweep_every_s = sweep_every_s
        self._min_eval_interval_s = min_eval_interval_s
        self._clock = clock
        self._active: set[str] = set()  # symbols with active alerts
        self._latest: dict[str, Quote] = {}
        self._dirty: set[str] = set()
        self._last_eval: dict[str, datetime] = {}
        self._wake = asyncio.Event()

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
        """Keep the latest quote of a symbol with active alerts and mark it dirty."""
        if symbol not in self._active:
            return
        self._latest[symbol] = quote
        self._dirty.add(symbol)
        self._wake.set()

    async def drain(self) -> float | None:
        """Evaluate the dirty symbols that are not throttled, each with its latest quote.

        Returns the seconds until a throttled symbol becomes due, or None.
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
            try:
                await self.on_tick(symbol, quote)
            except Exception:
                log.exception("alert evaluation failed for %s", symbol)
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
                log.exception("alert sweep failed")
            await asyncio.sleep(self._sweep_every_s)

    # -- passes ----------------------------------------------------------------------

    def _throttle_remaining(self, symbol: str, now: datetime) -> float:
        last = self._last_eval.get(symbol)
        if last is None:
            return 0.0
        return self._min_eval_interval_s - (now - last).total_seconds()

    async def on_tick(self, symbol: str, quote: Quote) -> int:
        """Evaluate `symbol`'s active alerts at `quote`; number triggered."""
        self._last_eval[symbol] = self._clock()
        done = await self._evaluate(symbol, quote)
        if done:
            await self._refresh_active()
        return done

    async def sweep(self) -> int:
        """Evaluate every symbol with active alerts at its current quote."""
        await self._refresh_active()
        quotes = await self._quotes(sorted(self._active))
        done = 0
        for symbol, quote in sorted(quotes.items()):
            try:
                done += await self._evaluate(symbol, quote)
            except Exception:
                log.exception("alert evaluation failed for %s", symbol)
        if done:
            await self._refresh_active()
        return done

    async def _refresh_active(self) -> None:
        async with self._sm() as session:
            self._active = set(await session.scalars(
                select(Alert.symbol).where(Alert.status == ACTIVE).distinct()
            ))

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
                log.debug("no quote for %s: %s", symbol, e.code)
        return quotes

    async def _evaluate(self, symbol: str, quote: Quote) -> int:
        price = reference_price(quote)
        if price is None:
            return 0
        async with self._sm() as session:
            rows = (await session.execute(
                select(Alert.id, Alert.condition, Alert.value, Alert.ref_price)
                .where(Alert.symbol == symbol, Alert.status == ACTIVE)
                .order_by(Alert.created_at, Alert.id)
            )).all()
        done = 0
        for c in (_Candidate(*row) for row in rows):
            if not is_hit(c.condition, c.value, c.ref_price, price):
                continue
            try:
                if await self._trigger(c.id, price):
                    done += 1
            except Exception:
                log.exception("triggering alert %s failed", c.id)
        return done

    async def _trigger(self, alert_id: UUID, price: Decimal) -> bool:
        async with self._sm() as session:
            try:
                alert = await session.scalar(
                    select(Alert).where(Alert.id == alert_id, Alert.status == ACTIVE)
                    .with_for_update(skip_locked=True)
                )
                if alert is None or not is_hit(
                    alert.condition, alert.value, alert.ref_price, price
                ):
                    await session.rollback()
                    return False
                alert.status = TRIGGERED
                alert.triggered_at = self._clock()
                alert.triggered_price = price
                await notify(
                    session, portfolio_id=None, kind="price_alert",
                    agent_id=str(alert.agent_id),
                    payload={
                        "alert_id": str(alert.id), "symbol": alert.symbol,
                        "condition": alert.condition.value,
                        "value": decimal_to_str(alert.value), "price": decimal_to_str(price),
                    },
                )
                await untrack_if_no_active_alert(session, alert.symbol)
                await session.commit()
            except BaseException:
                await session.rollback()
                discard_post_commit(session)
                raise
        await dispatch_post_commit(session, self._redis)
        return True
