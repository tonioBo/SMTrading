"""Keeps real-time quotes flowing into Redis for every tracked symbol.

Tracking: each source (orders, positions, …) keeps its own Redis set `md:tracked:{source}`;
`md:tracked` is the union. A symbol stays tracked while at least one source follows it.

StreamManager: one stream task per (provider, asset class) with the current symbol set,
restarted when the set changes (the old socket is closed before the new one opens). A
failing stream reconnects with exponential backoff (1, 2, 4, … up to `backoff_max_s`); a
stream the provider refuses for a while (StreamRefused: Alpaca's 406 «connection limit
exceeded», one socket per key) waits the provider's `retry_after_s` instead, with a note
in the published status. Until a stream has delivered its first quote, and while it is
down, its symbols are polled, as are symbols of classes without a stream provider.

One owner: a manager streams and polls only while it holds OWNER_KEY in Redis (renewed at
every reconcile, released on stop), so a second worker or a deploy overlap never opens a
second socket with the same key; the other one takes over when the owner's lease lapses.
Polling skips symbols whose market is closed, refreshing them only every
`closed_poll_every_s`.

Every stream tick passes the router's quote quality guard (marketdata.quality) before it
is written: a tick that jumps or disagrees with another provider is written marked
`suspect` (fills and stops wait), and the symbol is polled through the router at once
(at most every CONFIRM_EVERY_S), whose provider consensus confirms or replaces it.

Automatic rules (intraday mode B, source `auto_rules`): under a provider's symbol cap, up to
AUTO_RULES_RESERVE rule-only symbols rank before the watchlist-only ones (never before
orders, positions, alerts or the portal); the stream also asks the provider for 1-minute
bars of the rule symbols on it (`supports_stream_bars`), published to `bars1m:{symbol}`
(marketdata.minute_bars) outside the quality guard. A «symbol limit» refusal with bars
asked restarts the same symbols without bars (never fewer symbols). Without rule symbols
nothing differs from the plain quote stream.
"""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.common.errors import SMTError
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata.cache import write_quote
from smtrading.marketdata.minute_bars import StreamBar, publish_minute_bar
from smtrading.marketdata.provider import (
    DataProvider,
    ProviderUnsupported,
    StreamRefused,
    StreamSymbolLimit,
)
from smtrading.marketdata.router import MarketDataRouter

log = logging.getLogger(__name__)

SOURCES = frozenset({"orders", "positions", "alerts", "watchlists", "admin", "auto_rules"})
TRACKED_KEY = "md:tracked"
STATUS_KEY = "md:stream_status"
STATUS_TTL_S = 30  # a stale status (worker down) disappears instead of lying
OWNER_KEY = "md:stream_owner"
OWNER_TTL_S = 30
CONNECTING = "connecting"
CONNECTED = "connected"
BACKOFF = "backoff"
CLOSED_POLL_EVERY_S = 300
CONFIRM_EVERY_S = 30
# stream slots kept for automatic-rule symbols, taken only from watchlist-only symbols
AUTO_RULES_RESERVE = int(os.environ.get("SMT_AUTO_RULES_STREAM_RESERVE", "10"))
BARS_REFUSED_NOTE = ("Barre al minuto rifiutate dal fornitore: regole automatiche senza "
                     "volume e VWAP")


def _source_key(source: str) -> str:
    if source not in SOURCES:
        raise ValueError(f"unknown tracking source {source!r}")
    return f"{TRACKED_KEY}:{source}"


# renew the lease only if it is still ours; take it when free. KEYS = [owner], ARGV = [me, ttl]
_OWN_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == false or cur == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
return 0
"""
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


# Atomic: remove from the source set, then drop from the union only when no other source
# still follows the symbol. KEYS = [source set, union, *other source sets]; ARGV = symbols.
_UNTRACK_LUA = """
for _, sym in ipairs(ARGV) do
  redis.call('SREM', KEYS[1], sym)
  local followed = false
  for i = 3, #KEYS do
    if redis.call('SISMEMBER', KEYS[i], sym) == 1 then
      followed = true
      break
    end
  end
  if not followed then
    redis.call('SREM', KEYS[2], sym)
  end
end
return 0
"""


async def track(redis, symbols: list[str], *, source: str) -> None:
    key = _source_key(source)
    syms = sorted({canonical_symbol(s) for s in symbols})
    if not syms:
        return
    async with redis.pipeline(transaction=True) as pipe:
        pipe.sadd(key, *syms)
        pipe.sadd(TRACKED_KEY, *syms)
        await pipe.execute()


async def untrack(redis, symbols: list[str], *, source: str) -> None:
    key = _source_key(source)
    syms = sorted({canonical_symbol(s) for s in symbols})
    if not syms:
        return
    others = [_source_key(s) for s in sorted(SOURCES) if s != source]
    await redis.eval(_UNTRACK_LUA, 2 + len(others), key, TRACKED_KEY, *others, *syms)


async def tracked_symbols(redis) -> set[str]:
    return {_s(m) for m in await redis.smembers(TRACKED_KEY)}


async def tracked_sources(redis) -> dict[str, list[str]]:
    """symbol -> the sources following it (sorted), from the per-source sets."""
    names = sorted(SOURCES)
    async with redis.pipeline(transaction=False) as pipe:
        for name in names:
            pipe.smembers(_source_key(name))
        members = await pipe.execute()
    out: dict[str, list[str]] = {}
    for name, syms in zip(names, members, strict=True):
        for sym in syms:
            out.setdefault(_s(sym), []).append(name)
    return dict(sorted(out.items()))


@dataclass
class _Stream:
    provider: DataProvider
    asset_class: AssetClass
    items: tuple[tuple[str, str], ...]
    bars: tuple[str, ...] = ()  # provider symbols of the items asked for 1-minute bars
    state: str = CONNECTING
    note: str | None = None  # why it is refused (StreamRefused), for «Salute del sistema»
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def symbols(self) -> list[str]:
        return sorted(s for s, _ in self.items)

    @property
    def bar_symbols(self) -> list[str]:
        bars = set(self.bars)
        return sorted(s for s, ps in self.items if ps in bars)


class StreamManager:
    def __init__(
        self,
        router: MarketDataRouter,
        redis,
        sessionmaker: async_sessionmaker,
        *,
        reconcile_every_s: float = 5,
        poll_every_s: float = 5,
        backoff_max_s: float = 60,
        closed_poll_every_s: float = CLOSED_POLL_EVERY_S,
        owner_ttl_s: float = OWNER_TTL_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._router = router
        self._redis = redis
        self._sm = sessionmaker
        self._reconcile_every_s = reconcile_every_s
        self._poll_every_s = poll_every_s
        self._backoff_max_s = backoff_max_s
        self._closed_poll_every_s = closed_poll_every_s
        self._now = now
        self._streams: dict[str, _Stream] = {}
        # symbols without a stream provider (or unmappable by it), by asset class
        self._poll_only: dict[AssetClass, list[str]] = {}
        self._unknown: set[str] = set()
        # stream key -> symbol cap learnt from a «symbol limit» refusal (halved each time)
        self._caps: dict[str, int] = {}
        # stream key -> the provider refused the set with bars (asked again only once no
        # rule symbol is left on that stream)
        self._bars_refused: dict[str, bool] = {}
        self._markets: dict[str, str] = {}  # tracked symbol -> instrument market
        self._last_poll: dict[str, datetime] = {}
        self._confirming: dict[str, float] = {}  # symbol -> loop time of the last confirm
        self._confirm_tasks: set[asyncio.Task] = set()
        self._id = uuid.uuid4().hex
        self._owner_ttl_ms = max(int(owner_ttl_s * 1000), 1)
        self._owner = False

    # ---------------------------------------------------------------- public

    async def run(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._loop(self.reconcile, self._reconcile_every_s, "reconcile"))
                tg.create_task(self._loop(self.poll, self._poll_every_s, "poll"))
        finally:
            await self._stop(list(self._streams))
            await self._release()

    def status(self) -> dict:
        return {
            "streams": {key: self._stream_status(s) for key, s in sorted(self._streams.items())},
            "polling": sorted(s for group in self._polling().values() for s in group),
            "owner": self._owner,
        }

    @staticmethod
    def _stream_status(s: _Stream) -> dict:
        out = {"symbols": s.symbols, "state": s.state, "note": s.note}
        if s.bars:  # only with rule symbols on the stream: otherwise the status of before
            out["bars"] = s.bar_symbols
        return out

    # ---------------------------------------------------------------- loops

    @staticmethod
    async def _loop(step, every_s: float, what: str) -> None:
        while True:
            try:
                await step()
            except Exception:
                log.warning("stream manager %s failed; retrying", what, exc_info=True)
            await asyncio.sleep(every_s)

    async def _claim(self) -> bool:
        try:
            got = await self._redis.eval(_OWN_LUA, 1, OWNER_KEY, self._id,
                                         self._owner_ttl_ms)
        except Exception:
            log.warning("stream owner lease not renewed", exc_info=True)
            return self._owner
        owner = bool(int(got))
        if owner != self._owner:
            log.info("stream manager %s: %s", self._id[:8],
                     "owner, streaming" if owner else "another worker streams: standing by")
        self._owner = owner
        return owner

    async def _release(self) -> None:
        if not self._owner:
            return
        self._owner = False
        with contextlib.suppress(Exception):
            await self._redis.eval(_RELEASE_LUA, 1, OWNER_KEY, self._id)

    async def reconcile(self) -> None:
        if not await self._claim():
            await self._stop(list(self._streams))  # never a second socket on the key
            self._poll_only = {}
            return
        instrs = await self._instruments(await tracked_symbols(self._redis))
        sources = await tracked_sources(self._redis)

        auto_only = sorted(
            s for s, src in sources.items()
            if set(src) in ({"auto_rules"}, {"auto_rules", "watchlists"})
        )
        reserved = set(auto_only[:AUTO_RULES_RESERVE])

        def rank(symbol: str) -> tuple[int, str]:
            src = set(sources.get(symbol, []))
            if src - {"watchlists", "auto_rules"}:  # orders, positions, alerts, portal
                return (0, symbol)
            if symbol in reserved:  # the automatic rules' quota
                return (1, symbol)
            if "auto_rules" in src and "watchlists" not in src:  # rules over the quota
                return (3, symbol)
            return (2, symbol)  # watchlist-only (and untagged) symbols, as before

        def wants_bars(symbol: str) -> bool:
            return "auto_rules" in sources.get(symbol, [])

        self._markets = {i.symbol: i.market for i in instrs}
        self._last_poll = {s: t for s, t in self._last_poll.items() if s in self._markets}
        by_class: dict[AssetClass, list[Instrument]] = {}
        for i in instrs:
            by_class.setdefault(i.asset_class, []).append(i)

        wanted: dict[str, tuple[DataProvider, AssetClass, tuple[tuple[str, str], ...],
                                tuple[str, ...]]] = {}
        poll_only: dict[AssetClass, list[str]] = {}
        for asset_class, group in by_class.items():
            provider = await self._stream_provider(asset_class)
            items: list[tuple[str, str]] = []
            for i in group:
                ps = self._map(provider, i) if provider is not None else None
                if ps is None:
                    poll_only.setdefault(asset_class, []).append(i.symbol)
                else:
                    items.append((i.symbol, ps))
            if items:
                key = f"{provider.name}:{asset_class}"
                cap = self._caps.get(key, provider.stream_symbol_limit)
                if cap is not None and len(items) > cap:
                    items.sort(key=lambda it: rank(it[0]))
                    poll_only.setdefault(asset_class, []).extend(s for s, _ in items[cap:])
                    items = items[:cap]
                items.sort()
                bars: tuple[str, ...] = ()
                if provider.supports_stream_bars:
                    bars = tuple(sorted(ps for s, ps in items if wants_bars(s)))
                    if not bars:
                        self._bars_refused.pop(key, None)  # no rule left: ask again later
                    elif self._bars_refused.get(key):
                        bars = ()
                wanted[key] = (provider, asset_class, tuple(items), bars)

        # restarted only when its symbols or its bars change (a rule activated or paused)
        stale = [
            key for key, s in self._streams.items()
            if key not in wanted or wanted[key][2:] != (s.items, s.bars)
        ]
        await self._stop(stale)
        for key, (provider, asset_class, items, bars) in wanted.items():
            if key not in self._streams:
                stream = _Stream(provider, asset_class, items, bars)
                if self._bars_refused.get(key):
                    stream.note = BARS_REFUSED_NOTE
                stream.task = asyncio.create_task(self._run_stream(key, stream))
                self._streams[key] = stream
        self._poll_only = poll_only
        await self._redis.set(STATUS_KEY, json.dumps(self.status()), ex=STATUS_TTL_S)

    async def poll(self) -> None:
        if not self._owner:
            return
        now = self._now()
        groups = {}
        for asset_class, symbols in self._polling().items():
            due = [s for s in symbols if self._poll_due(s, now)]
            if due:
                groups[asset_class] = due
                for s in due:
                    self._last_poll[s] = now
        await asyncio.gather(*(self._poll_group(ac, syms) for ac, syms in groups.items()))

    # ---------------------------------------------------------------- internals

    def _poll_due(self, symbol: str, now: datetime) -> bool:
        """Open market (or unknown): always. Closed: at most every closed_poll_every_s."""
        market = self._markets.get(symbol)
        if market is not None:
            try:
                is_open = market_status(market, now).is_open
            except Exception:  # noqa: BLE001 - unknown market: keep polling
                is_open = True
            if not is_open:
                last = self._last_poll.get(symbol)
                return last is None or (now - last).total_seconds() >= self._closed_poll_every_s
        return True

    async def _poll_group(self, asset_class: AssetClass, symbols: list[str]) -> None:
        try:
            # the router writes every fetched quote to the cache (write_quote)
            await self._router.get_quotes(symbols, max_age_s=0)
        except SMTError as e:
            log.warning("polling %s %s failed: %s", asset_class, symbols, e)
        except Exception:
            log.warning("polling %s %s failed", asset_class, symbols, exc_info=True)

    def _polling(self) -> dict[AssetClass, list[str]]:
        groups = {ac: list(syms) for ac, syms in self._poll_only.items()}
        for s in self._streams.values():
            if s.state != CONNECTED:
                groups.setdefault(s.asset_class, []).extend(s.symbols)
        return {ac: sorted(syms) for ac, syms in groups.items() if syms}

    async def _instruments(self, symbols: set[str]) -> list[Instrument]:
        if not symbols:
            found: list[Instrument] = []
        else:
            async with self._sm() as session:
                found = list(
                    await session.scalars(
                        select(Instrument).where(
                            Instrument.symbol.in_(symbols), Instrument.active.is_(True)
                        )
                    )
                )
        unknown = symbols - {i.symbol for i in found}
        if unknown - self._unknown:
            log.warning("tracked symbols not in instruments, ignored: %s", sorted(unknown))
        self._unknown = unknown
        return found

    async def _stream_provider(self, asset_class: AssetClass) -> DataProvider | None:
        try:
            return await self._router.provider_for_stream(asset_class)
        except SMTError as e:
            log.warning("no stream provider for %s: %s", asset_class, e)
            return None

    @staticmethod
    def _map(provider: DataProvider, i: Instrument) -> str | None:
        try:
            return provider.map_symbol(
                i.symbol, i.asset_class, (i.provider_symbols or {}).get(provider.name)
            )
        except ProviderUnsupported:
            return None

    async def _stop(self, keys: list[str]) -> None:
        tasks = []
        for key in keys:
            stream = self._streams.pop(key)
            if stream.task is not None:
                stream.task.cancel()
                tasks.append(stream.task)
        if tasks:
            # asyncio.wait never raises the tasks' CancelledError, but still propagates
            # our own cancellation
            await asyncio.wait(tasks)

    def _confirm(self, symbol: str) -> None:
        """Poll a suspect symbol through the router (provider consensus), in the
        background, at most every CONFIRM_EVERY_S."""
        now = asyncio.get_running_loop().time()
        if now - self._confirming.get(symbol, -1e18) < CONFIRM_EVERY_S:
            return
        self._confirming[symbol] = now
        task = asyncio.create_task(self._router.get_quotes([symbol], max_age_s=0))
        self._confirm_tasks.add(task)
        task.add_done_callback(self._confirmed)

    def _confirmed(self, task: asyncio.Task) -> None:
        self._confirm_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("confirming a suspect quote failed: %s", task.exception())

    async def _run_stream(self, key: str, stream: _Stream) -> None:
        delay = 1.0
        while True:
            stream.state = CONNECTING  # polled until the first quote proves the stream live
            delivered = False
            try:
                # bars passed only when asked: the call of before for every other stream
                gen = (stream.provider.stream(list(stream.items), bars=frozenset(stream.bars))
                       if stream.bars else stream.provider.stream(list(stream.items)))
                async with contextlib.aclosing(gen) as quotes:
                    async for q in quotes:
                        if not delivered:
                            delivered = True
                            stream.state = CONNECTED
                            stream.note = (BARS_REFUSED_NOTE if self._bars_refused.get(key)
                                           else None)
                        if isinstance(q, StreamBar):  # no quality guard, no quote written
                            try:
                                await publish_minute_bar(self._redis, q)
                            except Exception:
                                log.warning("stream %s: publishing the bar of %s failed", key,
                                            q.symbol, exc_info=True)
                            continue
                        q = await self._router.guard.check_stream(q, stream.asset_class)
                        if q.suspect is not None:
                            self._confirm(q.symbol)
                        try:
                            await write_quote(self._redis, q)
                        except Exception:
                            log.warning("stream %s: writing %s failed", key, q.symbol,
                                        exc_info=True)
                raise ConnectionError("stream ended")
            except StreamSymbolLimit as e:
                if stream.bars:  # with bars: the same symbols again, without bars
                    self._bars_refused[key] = True
                    stream.state = BACKOFF
                    stream.note = BARS_REFUSED_NOTE
                    log.warning("stream %s: symbol limit with minute bars (%s); restarting "
                                "without bars", key, e)
                    return  # the next reconcile restarts it without bars
                # too many symbols: the next set carries half
                cap = max(1, len(stream.items) // 2)
                self._caps[key] = cap
                stream.state = BACKOFF
                stream.note = (f"Troppi simboli per il provider ({e}): {cap} in tempo reale, "
                               f"gli altri in polling")
                log.warning("stream %s: symbol limit (%s); restarting with %s symbols",
                            key, e, cap)
                return  # the next reconcile restarts it with the smaller set
            except StreamRefused as e:  # refused for a while: the provider's own wait
                stream.state = BACKOFF
                stream.note = (f"Rifiutato dal provider ({e}): prezzi in polling, nuovo "
                               f"tentativo tra {e.retry_after_s:g} s")
                log.warning("stream %s refused (%s); polling, reconnecting in %ss",
                            key, e, e.retry_after_s)
                await asyncio.sleep(e.retry_after_s)
                delay = 1.0
                continue
            except Exception as e:  # noqa: BLE001 - any failure: poll and reconnect later
                if delivered:
                    delay = 1.0
                log.warning("stream %s failed (%s); reconnecting in %ss", key, e, delay)
            stream.state = BACKOFF
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff_max_s)
