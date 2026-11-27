"""The `scalper` (intraday mode B): the active rules in memory, each with its RuleRunner,
fed by the worker's price publication (`autorules.feed`).

Every `reload_s` one `active_rules` query: with no active rule nothing else happens (no
subscription, no provider call, no Redis call but the job heartbeat); otherwise the rule
symbols are tracked again for the price stream (idempotent: a pause/activate race cannot
leave a rule unfollowed), the feed follows exactly them, a new rule or a changed version
gets a new runner warmed up from the minute history, a rule gone is dropped (its open
trades stay with the Tracker). A bar goes to the runners of its symbol; a quote too, and
an entry intent goes to the executor. The guards before a send live in the executor."""
import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smtrading.autorules.feed import (
    PriceFeed,
    read_stream_status,
    stream_bars,
    stream_state,
)
from smtrading.autorules.quotes import CacheQuoteSource
from smtrading.autorules.rules import STREAM_SOURCE, ActiveRule, active_rules
from smtrading.autorules.runner import Entry, RuleRunner
from smtrading.marketdata.minute_bars import StreamBar
from smtrading.marketdata.streaming import track
from smtrading.marketdata.types import Quote
from smtrading.worker.jobstate import job_tick

log = logging.getLogger(__name__)

SCALPER_JOB = "scalper"
SESSION_EXIT_MARGIN_MIN = 2  # the rule exits this long before the worker's day close


def _runner_inputs(rule: ActiveRule) -> tuple:
    """What a runner is built from: a change means a new runner."""
    return rule.version, rule.model, rule.market, rule.tick_size, rule.day_close_minutes


def _price(q: Quote) -> Decimal | None:
    """The last trade, otherwise the mid."""
    if q.last is not None:
        return q.last
    if q.bid is not None and q.ask is not None:
        return (q.bid + q.ask) / 2
    return None


class Scalper:
    def __init__(self, sessionmaker, redis, router, *,
                 clock: Callable[[], datetime] | None = None, reload_s: float = 5,
                 executor_factory=None) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reload_s = reload_s
        self.qs = CacheQuoteSource(redis, router)
        self._feed = PriceFeed(redis, router, clock=self._clock)
        self._executor = (executor_factory(sessionmaker, redis, self.qs, clock=self._clock)
                          if executor_factory is not None else None)
        self._rules: dict[uuid.UUID, tuple[ActiveRule, RuleRunner]] = {}
        self._by_symbol: dict[str, list[tuple[ActiveRule, RuleRunner]]] = {}
        self.stream_status: dict | None = None  # md:stream_status at the last refresh
        self._feed_notes: dict[str, tuple[str, bool]] = {}

    def runners(self, symbol: str) -> list[tuple[ActiveRule, RuleRunner]]:
        return list(self._by_symbol.get(symbol, ()))

    def _index(self) -> None:
        by: dict[str, list[tuple[ActiveRule, RuleRunner]]] = {}
        for rule, runner in self._rules.values():
            by.setdefault(rule.symbol, []).append((rule, runner))
        self._by_symbol = by

    # --- the active rules --------------------------------------------------------

    async def reload(self) -> None:
        async with self._sm() as session:
            active = await active_rules(session)
        if not active:
            if self._rules or self._feed.symbols:
                log.info("no active automatic rule: scalper idle")
                self._rules.clear()
                self._index()
                self._feed_notes.clear()
                await self._feed.set_symbols(set())
            await job_tick({"active_rules": 0})
            return
        symbols = {r.symbol for r in active}
        await track(self._redis, sorted(symbols), source=STREAM_SOURCE)
        await self._feed.set_symbols(symbols)
        for rule in active:
            old = self._rules.get(rule.rule_id)
            if old is not None and _runner_inputs(old[0]) == _runner_inputs(rule):
                self._rules[rule.rule_id] = (rule, old[1])
                continue
            runner = RuleRunner(
                rule.model, rule.params, market=rule.market, tick_size=rule.tick_size,
                session_exit_before=timedelta(
                    minutes=rule.day_close_minutes + SESSION_EXIT_MARGIN_MIN))
            await self._feed.warm_up(rule, runner, self._clock())
            # registered right after its own warm-up (no await in between): no bar
            # published after the warm-up's read is missed
            self._rules[rule.rule_id] = (rule, runner)
            self._index()
            log.info("automatic rule %s v%s on %s: runner ready (%s bars)", rule.rule_id,
                     rule.version, rule.symbol, runner.series.count())
        gone = self._rules.keys() - {r.rule_id for r in active}
        for rule_id in gone:
            log.info("automatic rule %s no longer active: runner dropped", rule_id)
            del self._rules[rule_id]
        self._index()
        self.stream_status = await read_stream_status(self._redis)
        self._note_feed(symbols)
        await job_tick({"active_rules": len(self._rules)})

    def _note_feed(self, symbols: set[str]) -> None:
        """Log, once per change, a rule symbol not on a connected stream or without bars."""
        for symbol in sorted(symbols):
            seen = (stream_state(self.stream_status, symbol),
                    stream_bars(self.stream_status, symbol))
            if self._feed_notes.get(symbol) != seen:
                self._feed_notes[symbol] = seen
                if seen != ("stream", True):
                    log.warning("automatic rule symbol %s: prices %s, minute bars %s",
                                symbol, seen[0], "yes" if seen[1] else "no")
        for symbol in set(self._feed_notes) - symbols:
            del self._feed_notes[symbol]

    # --- the prices --------------------------------------------------------------

    async def on_event(self, ev: Quote | StreamBar) -> None:
        if isinstance(ev, StreamBar):
            for _, runner in self.runners(ev.symbol):
                runner.on_bar(ev.bar)
            return
        price = _price(ev)
        if price is None:
            return
        for rule, runner in self.runners(ev.symbol):
            intent = runner.on_price(ev.ts, price, ev.bid, ev.ask)
            if isinstance(intent, Entry) and self._executor is not None:
                await self._executor.enter(rule, runner, intent, ev)
            # an Exit is the Tracker's (open trades), not the entry path

    # --- loops -------------------------------------------------------------------

    async def _reload_loop(self) -> None:
        while True:
            try:
                await self.reload()
            except Exception:
                log.warning("scalper refresh failed; retrying", exc_info=True)
            await asyncio.sleep(self._reload_s)

    async def _consume(self) -> None:
        async for ev in self._feed.events():
            try:
                await self.on_event(ev)
            except Exception:
                log.exception("scalper event of %s failed", ev.symbol)

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._reload_loop())
                tg.create_task(self._consume())
        finally:
            await self.close()

    async def close(self) -> None:
        await self._feed.close()
