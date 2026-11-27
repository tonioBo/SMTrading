"""The prices of the `scalper`, only from the internal publication of the worker's price
stream: the `ticks:{symbol}` pub/sub of `write_quote` and the `bars1m:{symbol}` Redis
streams of the minute bars. No data connection of its own: the only provider call is the
REST minute history of the warm-up (router cache and shared rate limit).

`PriceFeed` follows only the symbols of the active rules (`set_symbols`); with none it
holds no subscription, no reader and makes no Redis call. Two reader tasks feed one
queue: the ticks (a `Quote` per message) and the bars (`XREAD BLOCK` from the last entry
id seen, a `StreamBar` per entry). The subscription changes are applied by the tick
reader itself between two reads, so one connection is never read by two coroutines."""
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from smtrading.autorules.rules import ActiveRule
from smtrading.instruments.market_hours import CALENDARS, session_on
from smtrading.intraday.calendar import last_sessions
from smtrading.marketdata.minute_bars import (
    BARS_KEY,
    StreamBar,
    bar_from_fields,
    read_latest_minute_bars,
)
from smtrading.marketdata.serialization import loads_quote
from smtrading.marketdata.streaming import CONNECTED, STATUS_KEY
from smtrading.marketdata.types import Quote, Timeframe

log = logging.getLogger(__name__)

TICKS_CHANNEL = "ticks:{symbol}"
BARS_BLOCK_MS = 1000
BARS_READ_COUNT = 100
TICK_WAIT_S = 0.5  # the tick reader applies subscription changes at least this often
WARMUP_SLACK_MIN = 5
WARMUP_MAX_SESSIONS = 5  # sessions looked back for the warm-up (max warm-up: 245 min)

_UNREADABLE = (ValueError, KeyError, TypeError, ArithmeticError)  # a malformed payload

StreamState = Literal["stream", "polling", "none"]


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def _connected_streams(status: dict | None):
    for s in ((status or {}).get("streams") or {}).values():
        if isinstance(s, dict) and s.get("state") == CONNECTED:
            yield s


def stream_state(status: dict | None, symbol: str) -> StreamState:
    """From the stream manager's published status (`md:stream_status`): `stream` when the
    symbol is on a connected stream, `polling` when it is polled, otherwise `none` (no
    status: the worker is down or the status expired)."""
    if any(symbol in (s.get("symbols") or []) for s in _connected_streams(status)):
        return "stream"
    if symbol in ((status or {}).get("polling") or []):
        return "polling"
    return "none"


def stream_bars(status: dict | None, symbol: str) -> bool:
    """Minute bars of `symbol` asked on a connected stream (the key exists only with
    rule symbols on the stream)."""
    return any(symbol in s.get("bars", []) for s in _connected_streams(status))


async def read_stream_status(redis) -> dict | None:
    raw = await redis.get(STATUS_KEY)
    if raw is None:
        return None
    try:
        value = json.loads(_s(raw))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _id_key(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


def warmup_start(market: str, warmup: int, now: datetime) -> datetime:
    """Where the warm-up history starts. Markets with a calendar: the open of the oldest
    session needed so that the regular-session minutes up to `now` (the runner keeps
    only those) cover at least `warmup + 5` — today's open when it is enough, otherwise
    earlier sessions (before the open, in the evening, early in the session). Crypto and
    FX: midnight UTC, and at least `warmup + 5` clock minutes before `now`."""
    needed = timedelta(minutes=warmup + WARMUP_SLACK_MIN)
    if market not in CALENDARS:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return min(midnight, now - needed)
    covered = timedelta(0)
    start: datetime | None = None
    for day in reversed(last_sessions(market, now, WARMUP_MAX_SESSIONS)):
        session = session_on(market, day)
        if session is None or session[0] >= now:  # today's session not open yet
            continue
        covered += min(now, session[1]) - session[0]
        start = session[0]
        if covered >= needed:
            break
    return start if start is not None else now - needed


class PriceFeed:
    def __init__(self, redis, router, *, clock: Callable[[], datetime]) -> None:
        self._redis = redis
        self._router = router
        self._clock = clock
        self._symbols: frozenset[str] = frozenset()
        self._last_ids: dict[str, str] = {}  # bars1m: the last entry id seen per symbol
        self._queue: asyncio.Queue[Quote | StreamBar] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._pubsub = None
        self._subscribed: set[str] = set()

    @property
    def symbols(self) -> frozenset[str]:
        return self._symbols

    def _advance(self, symbol: str, entry_id: str) -> None:
        cur = self._last_ids.get(symbol)
        if cur is None or _id_key(entry_id) > _id_key(cur):
            self._last_ids[symbol] = entry_id

    async def set_symbols(self, symbols: set[str]) -> None:
        """Follow exactly `symbols`: the bars from the current end of each new symbol's
        stream (the warm-up reads what is already there); none → every reader stopped."""
        wanted = frozenset(symbols)
        if wanted == self._symbols:
            return
        for symbol in sorted(wanted - self._symbols):
            last = await self._redis.xrevrange(BARS_KEY.format(symbol=symbol), count=1)
            self._last_ids[symbol] = _s(last[0][0]) if last else "0-0"
        for symbol in self._symbols - wanted:
            self._last_ids.pop(symbol, None)
        self._symbols = wanted
        if not wanted:
            await self.close()
            return
        if not self._tasks:
            self._pubsub = self._redis.pubsub()
            await self._sync_subscriptions()
            self._tasks = [asyncio.create_task(self._read_ticks()),
                           asyncio.create_task(self._read_bars())]

    async def _sync_subscriptions(self) -> None:
        want = {TICKS_CHANNEL.format(symbol=s) for s in self._symbols}
        if add := sorted(want - self._subscribed):
            await self._pubsub.subscribe(*add)
        if drop := sorted(self._subscribed - want):
            await self._pubsub.unsubscribe(*drop)
        self._subscribed = want

    async def _read_ticks(self) -> None:
        while True:
            try:
                await self._sync_subscriptions()
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True,
                                                     timeout=TICK_WAIT_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("scalper tick read failed; retrying", exc_info=True)
                await asyncio.sleep(1)
                continue
            if not msg or msg.get("type") != "message":
                continue
            try:
                quote = loads_quote(_s(msg["data"]))
            except _UNREADABLE:
                log.warning("unreadable tick on %s", _s(msg.get("channel")))
                continue
            if quote.symbol in self._symbols:
                self._queue.put_nowait(quote)

    async def _read_bars(self) -> None:
        while True:
            streams = {BARS_KEY.format(symbol=s): self._last_ids.get(s, "0-0")
                       for s in sorted(self._symbols)}
            try:
                found = await self._redis.xread(streams, count=BARS_READ_COUNT,
                                                block=BARS_BLOCK_MS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("scalper bar read failed; retrying", exc_info=True)
                await asyncio.sleep(1)
                continue
            for key, entries in found or []:
                symbol = _s(key).split(":", 1)[1]
                if symbol not in self._symbols:
                    continue
                for entry_id, raw in entries:
                    self._advance(symbol, _s(entry_id))
                    try:
                        bar = bar_from_fields(raw)
                    except _UNREADABLE:
                        log.warning("unreadable minute bar of %s", symbol)
                        continue
                    self._queue.put_nowait(StreamBar(symbol, bar))

    async def warm_up(self, rule: ActiveRule, runner, now: datetime) -> None:
        """The REST minute history from `warmup_start` merged with the newest `bars1m`
        entries of the same span (the REST bar wins on a minute both have), all to
        `runner.on_bar` oldest first. Never `on_price`: a warm-up never fires. A history
        the provider cannot give leaves the stream's bars (or a live warm-up)."""
        start = warmup_start(rule.market, runner.warmup, now)
        try:
            history = await self._router.get_bars(rule.symbol, Timeframe.m1, start, now)
        except Exception:
            log.warning("minute history of %s unavailable: warm-up from the stream bars",
                        rule.symbol, exc_info=True)
            history = []
        entries = await read_latest_minute_bars(self._redis, rule.symbol)
        merged = {bar.ts: bar for _, bar in entries if start <= bar.ts < now}
        merged.update((bar.ts, bar) for bar in history)
        for ts in sorted(merged):
            runner.on_bar(merged[ts])
        if entries and rule.symbol in self._symbols:
            self._advance(rule.symbol, entries[-1][0])

    async def events(self) -> AsyncIterator[Quote | StreamBar]:
        while True:
            yield await self._queue.get()

    async def close(self) -> None:
        """Stop the readers and drop the subscription (idempotent)."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        pubsub, self._pubsub = self._pubsub, None
        self._subscribed = set()
        self._symbols = frozenset()
        self._last_ids.clear()
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
