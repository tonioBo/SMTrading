"""Quote quality guard (plan 25): never pass a bad price silently to money logic.

Every quote a provider returns (router `get_quotes`, stream ticks) is classified:

- `wide_spread`: bid/ask wider than MAX_SPREAD of the mid (after hours, thin books: the
  mid can sit far from any trade, e.g. NVDA bid 145.6 / ask 178.29). Counted only: the
  valuation marks such a quote at its last trade.
- `jump`: its price moved more than JUMP_PCT (by asset class) from the last accepted
  price of the symbol, if that is at most JUMP_WINDOW_S old.
- `disagree`: another provider's price of the symbol, at most DISAGREE_WINDOW_S old,
  differs by more than DISAGREE_PCT.
- `stale`: older than the «quota vecchia» threshold while its market is open. Counted
  only: the consumers already refuse stale quotes.

A `jump` or `disagree` quote is not accepted alone: the router asks the next providers
and takes the consensus (the first quote another source agrees with, within
DISAGREE_PCT, or the first one with no problem). Unconfirmed, it is passed on marked
`suspect` (the reason): order fills, stop triggers and market orders refuse it and wait
for the next good quote. A lone source's new level is accepted (a genuine gap must reach
the stops in bounded time) as soon as either:
- the run's quotes have all stayed beyond the jump limit on the same side of the last
  accepted price for CONFIRM_JUMP_S: the next one (newer quote time) is accepted, while
  no other recent source disagrees (a gap that holds, or keeps going, is not a glitch); or
- the last accepted price ages out of JUMP_WINDOW_S.

Redis (fails open: a Redis error never blocks a quote):
- `mdq:ref:{sym}` the last accepted price (JSON {p, at}), TTL JUMP_WINDOW_S;
- `mdq:prov:{sym}` hash provider -> "price|epoch" of accepted quotes;
- `mdq:ev:{YYYYMMDDHH}` hash "provider|symbol|reason" -> count (hourly, kept 25 h);
- `mdq:suspects` sorted set of suspect quotes (score: epoch), trimmed to 24 h;
- `mdq:suspect_since:{sym}` when the symbol's current suspect run began (cleared by the
  next accepted quote): the Telegram alarm (`SuspectAlarm`) reads it;
- `mdq:pending:{sym}` the first quote of the suspect run (JSON {p, at, ts}: price, when
  seen, the quote time); cleared by an accepted quote, replaced by a suspect quote on the
  other side of the last accepted price.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.types import Quote

log = logging.getLogger(__name__)

OK, WIDE_SPREAD, JUMP, DISAGREE, STALE = "ok", "wide_spread", "jump", "disagree", "stale"
REASONS = (WIDE_SPREAD, JUMP, DISAGREE, STALE)
SUSPECT_REASON = "suspect_quote"  # details.reason of the STALE_QUOTE error on a suspect

# a spread wider than this share of the mid is not a fair price (valuation.mark_price)
MAX_SPREAD = Decimal("0.01")
JUMP_PCT: dict[str, Decimal] = {
    "us_equity": Decimal("0.15"), "eu_equity": Decimal("0.15"),
    "crypto": Decimal("0.25"), "forex": Decimal("0.05"),
}
JUMP_WINDOW_S = 30 * 60
DISAGREE_PCT = Decimal("0.03")
DISAGREE_WINDOW_S = 120
CONFIRM_JUMP_S = 120  # a lone source's jump holding this long is accepted
SUSPECT_ALERT_AFTER_S = 15 * 60
EVENTS_KEEP_S = 25 * 3600
SUSPECTS_KEEP_S = 24 * 3600
REF_WRITE_EVERY_S = 5.0  # stream ticks refresh the accepted price at most this often
STREAM_NOTE_EVERY_S = 60.0  # a lasting stream suspect is counted once a minute

REF_KEY = "mdq:ref:{}"
PROV_KEY = "mdq:prov:{}"
EVENTS_KEY = "mdq:ev:{}"
SUSPECTS_KEY = "mdq:suspects"
SINCE_KEY = "mdq:suspect_since:{}"
PENDING_KEY = "mdq:pending:{}"


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


def is_wide(q: Quote) -> bool:
    if q.bid is None or q.ask is None:
        return False
    mid = (q.bid + q.ask) / 2
    return mid <= 0 or (q.ask - q.bid) > mid * MAX_SPREAD


def price_of(q: Quote) -> Decimal | None:
    """The price the checks compare: the mid of a tight quote, else the last trade (like
    valuation.mark_price), else whatever side there is. None when there is no price."""
    if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask > 0 \
            and (not is_wide(q) or q.last is None):
        return (q.bid + q.ask) / 2
    for p in (q.last, q.bid, q.ask):
        if p is not None and p > 0:
            return p
    return None


def moved(a: Decimal, b: Decimal) -> Decimal:
    """|a / b - 1|."""
    return abs(a / b - 1) if b > 0 else Decimal(0)


@dataclass(frozen=True)
class Seen:
    """A price of the symbol seen at epoch `at` (`provider` for another source's)."""

    price: Decimal
    at: float
    provider: str | None = None
    ts: float | None = None  # the quote time (epoch), for a pending level


@dataclass
class Context:
    """What the checks compare a new quote with."""

    ref: Seen | None = None
    others: list[Seen] = field(default_factory=list)
    pending: Seen | None = None  # the first quote of the current suspect run (PENDING_KEY)


def same_side(p: Decimal, pending: Decimal, ref: Decimal) -> bool:
    return (p - ref) * (pending - ref) > 0


def confirms(q: Quote, p: Decimal, ref: Seen, pending: Seen | None, now: float) -> bool:
    """`q` (price `p`, a jump from `ref`) confirms the suspect run that began with
    `pending`: at least CONFIRM_JUMP_S later, a newer quote time, the same side of `ref`."""
    return (pending is not None and now - pending.at >= CONFIRM_JUMP_S
            and (pending.ts is None or q.ts.timestamp() > pending.ts)
            and same_side(p, pending.price, ref.price))


def problems(q: Quote, asset_class: AssetClass | str, ctx: Context | None, now: float
             ) -> list[str]:
    """`jump` and/or `disagree` (pure); [] when the quote can be accepted alone."""
    p = price_of(q)
    if p is None or ctx is None:
        return []
    out: list[str] = []
    ref = ctx.ref
    limit = JUMP_PCT.get(str(asset_class), Decimal("0.15"))
    if ref is not None and now - ref.at <= JUMP_WINDOW_S and moved(p, ref.price) > limit \
            and not confirms(q, p, ref, ctx.pending, now):
        out.append(JUMP)
    for o in ctx.others:
        if o.provider != q.provider and now - o.at <= DISAGREE_WINDOW_S \
                and moved(p, o.price) > DISAGREE_PCT:
            out.append(DISAGREE)
            break
    return out


def agrees(a: Quote, b: Quote) -> bool:
    pa, pb = price_of(a), price_of(b)
    return pa is not None and pb is not None and moved(pa, pb) <= DISAGREE_PCT


@dataclass
class Contest:
    """The candidates of one symbol whose first quote could not be accepted alone."""

    candidates: list[tuple[Quote, list[str]]] = field(default_factory=list)

    def offer(self, q: Quote, probs: list[str]) -> tuple[Quote, list[tuple[Quote, list[str]]]] | None:
        """Add `q`; (the quote to accept, the rejected candidates) once there is a
        consensus, else None. The first candidate another one agrees with wins; else
        a candidate with no problem of its own."""
        for c, _ in self.candidates:
            if agrees(c, q):
                losers = [x for x in self.candidates if x[0] is not c]
                return c, losers
        if not probs:
            return q, list(self.candidates)
        self.candidates.append((q, probs))
        return None

    def suspect(self) -> Quote:
        q, probs = self.candidates[0]
        return replace(q, suspect=probs[0])


def _hour_bucket(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%d%H")


class QuoteGuard:
    """Redis state of the checks. Every method fails open (logged)."""

    def __init__(self, redis, clock: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._clock = clock
        self._ref_written: dict[str, float] = {}
        self._noted: dict[tuple[str, str], float] = {}

    def now(self) -> float:
        return self._clock()

    async def contexts(self, symbols: list[str]) -> dict[str, Context]:
        if not symbols:
            return {}
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for s in symbols:
                    pipe.get(REF_KEY.format(s))
                    pipe.hgetall(PROV_KEY.format(s))
                    pipe.get(PENDING_KEY.format(s))
                raw = await pipe.execute()
        except Exception:
            log.warning("quote guard: context unreadable", exc_info=True)
            return {}
        out: dict[str, Context] = {}
        for i, s in enumerate(symbols):
            ctx = Context()
            ref, prov, pending = raw[3 * i], raw[3 * i + 1] or {}, raw[3 * i + 2]
            try:
                if ref:
                    d = json.loads(_s(ref))
                    ctx.ref = Seen(Decimal(d["p"]), float(d["at"]))
                if pending:
                    d = json.loads(_s(pending))
                    ctx.pending = Seen(Decimal(d["p"]), float(d["at"]), ts=float(d["ts"]))
                for name, v in prov.items():
                    price, _, at = _s(v).partition("|")
                    ctx.others.append(Seen(Decimal(price), float(at), _s(name)))
            except Exception:
                log.warning("quote guard: bad context for %s", s, exc_info=True)
                ctx = Context()
            out[s] = ctx
        return out

    async def accepted(self, quotes: list[Quote], *, throttle: bool = False) -> None:
        """Remember the accepted quotes (the next ones are compared with them) and end
        the symbols' suspect runs. `throttle` (stream ticks): the price is written at most
        every REF_WRITE_EVERY_S per symbol."""
        now = self._clock()
        todo = []
        for q in quotes:
            p = price_of(q)
            if p is None:
                continue
            if throttle and now - self._ref_written.get(q.symbol, 0.0) < REF_WRITE_EVERY_S:
                continue
            self._ref_written[q.symbol] = now
            todo.append((q, p))
        if not todo:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for q, p in todo:
                    pipe.set(REF_KEY.format(q.symbol), json.dumps({"p": str(p), "at": now}),
                             ex=JUMP_WINDOW_S)
                    pipe.hset(PROV_KEY.format(q.symbol), q.provider, f"{p}|{now}")
                    pipe.expire(PROV_KEY.format(q.symbol), 10 * DISAGREE_WINDOW_S)
                    pipe.delete(SINCE_KEY.format(q.symbol))
                    pipe.delete(PENDING_KEY.format(q.symbol))
                await pipe.execute()
        except Exception:
            log.warning("quote guard: accepted quotes not stored", exc_info=True)

    async def record(self, events: list[tuple[str, str, str]]) -> None:
        """Count (provider, symbol, reason) events in the hourly bucket."""
        if not events:
            return
        key = EVENTS_KEY.format(_hour_bucket(self._clock()))
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for provider, symbol, reason in events:
                    pipe.hincrby(key, f"{provider}|{symbol}|{reason}", 1)
                pipe.expire(key, EVENTS_KEEP_S)
                await pipe.execute()
        except Exception:
            log.warning("quote guard: events not recorded", exc_info=True)

    async def suspected(self, q: Quote) -> None:
        """A quote passed on as suspect: its run started (if not already), and counted
        and listed at most once a minute per symbol and reason."""
        now = self._clock()
        reason = q.suspect or JUMP
        try:
            await self._redis.set(SINCE_KEY.format(q.symbol), str(now), nx=True, ex=2 * 86400)
        except Exception:
            log.warning("quote guard: suspect run of %s not stored", q.symbol, exc_info=True)
        await self._pending(q, now)

        async def write() -> None:
            await self.record([(q.provider, q.symbol, reason)])
            try:
                async with self._redis.pipeline(transaction=False) as pipe:
                    pipe.zadd(SUSPECTS_KEY,
                              {f"{q.provider}|{q.symbol}|{reason}|{now:.6f}": now})
                    pipe.zremrangebyscore(SUSPECTS_KEY, "-inf", now - SUSPECTS_KEEP_S)
                    await pipe.execute()
            except Exception:
                log.warning("quote guard: suspect %s not recorded", q.symbol, exc_info=True)

        await self._note(q, reason, write)

    async def _pending(self, q: Quote, now: float) -> None:
        """The first quote of the suspect run (`confirms`): kept while the suspect quotes
        stay on its side of the last accepted price, replaced by one on the other side."""
        p = price_of(q)
        if p is None:
            return
        key = PENDING_KEY.format(q.symbol)
        try:
            raw, ref = await self._redis.mget([key, REF_KEY.format(q.symbol)])
            if raw:
                first = Decimal(json.loads(_s(raw))["p"])
                if ref is None or same_side(p, first, Decimal(json.loads(_s(ref))["p"])):
                    return
            await self._redis.set(key, json.dumps(
                {"p": str(p), "at": now, "ts": q.ts.timestamp()}), ex=JUMP_WINDOW_S)
        except Exception:
            log.warning("quote guard: pending level of %s not stored", q.symbol, exc_info=True)

    async def check_stream(self, q: Quote, asset_class: AssetClass | str) -> Quote:
        """A stream tick (one source, no fallback at hand): accepted, or marked suspect.
        Wide spreads are counted at most once a minute per symbol."""
        try:
            ctx = (await self.contexts([q.symbol])).get(q.symbol)
            probs = problems(q, asset_class, ctx, self._clock())
        except Exception:
            log.warning("quote guard: stream check of %s failed", q.symbol, exc_info=True)
            return q
        if is_wide(q):
            await self._note(q, WIDE_SPREAD, lambda: self.record(
                [(q.provider, q.symbol, WIDE_SPREAD)]))
        if not probs:
            await self.accepted([q], throttle=True)
            return q
        marked = replace(q, suspect=probs[0])
        await self.suspected(marked)
        return marked

    async def _note(self, q: Quote, reason: str, write) -> None:
        now = self._clock()
        key = (q.symbol, reason)
        if now - self._noted.get(key, -1e18) < STREAM_NOTE_EVERY_S:
            return
        self._noted[key] = now
        await write()


# -- reading back («Salute del sistema», the alarm) --------------------------------------

async def read_events(redis, now: float, hours: int = 24) -> dict[tuple[str, str, str], int]:
    """(provider, symbol, reason) -> count over the last `hours` hourly buckets."""
    keys = [EVENTS_KEY.format(_hour_bucket(now - 3600 * h)) for h in range(hours)]
    async with redis.pipeline(transaction=False) as pipe:
        for k in keys:
            pipe.hgetall(k)
        buckets = await pipe.execute()
    out: dict[tuple[str, str, str], int] = {}
    for b in buckets:
        for k, v in (b or {}).items():
            parts = _s(k).split("|")
            if len(parts) != 3:
                continue
            key = (parts[0], parts[1], parts[2])
            out[key] = out.get(key, 0) + int(_s(v))
    return out


async def last_accepted(redis, symbol: str) -> Decimal | None:
    """The last accepted price of `symbol` (at most JUMP_WINDOW_S old), or None."""
    raw = await redis.get(REF_KEY.format(symbol))
    if not raw:
        return None
    try:
        return Decimal(json.loads(_s(raw))["p"])
    except Exception:  # noqa: BLE001 - a garbled entry: none
        return None


async def read_suspects(redis, now: float) -> list[tuple[float, str, str, str]]:
    """(epoch, provider, symbol, reason) of the suspect quotes of the last 24 h."""
    raw = await redis.zrangebyscore(SUSPECTS_KEY, now - SUSPECTS_KEEP_S, "+inf",
                                    withscores=True)
    out = []
    for member, score in raw:
        parts = _s(member).split("|")
        if len(parts) >= 3:
            out.append((float(score), parts[0], parts[1], parts[2]))
    return out


async def suspect_since(redis, symbols: list[str]) -> dict[str, float]:
    """symbol -> epoch its current suspect run began, for the symbols in one."""
    if not symbols:
        return {}
    vals = await redis.mget([SINCE_KEY.format(s) for s in symbols])
    out = {}
    for s, v in zip(symbols, vals, strict=True):
        if v is not None:
            try:
                out[s] = float(_s(v))
            except ValueError:
                continue
    return out
