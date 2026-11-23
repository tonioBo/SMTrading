"""Which instruments the broker of a linked portfolio lets SMTrading trade (plan 44).

The adapters answer (`BrokerAdapter.tradability`, with their own rules: eToro search +
eligibility, Alpaca assets, Trading 212 instrument list); the answers are cached in Redis
per broker link, one hash `smt:broker_tradable:<link id>` of symbol -> JSON
{tradable, long_unleveraged, short, fractional, partial_close, code, at}, fresh for
FRESH_S (12 h) and kept KEEP_S. Unknown answers (the broker did not reply) are never
cached.

- `portfolio_tradability`: the check_tradable tool, the portal and preview_order: the
  cached answer, else the broker's (on demand, in chunks); a portfolio without a broker
  answers from the catalog.
- `cached_refusal`: placement (engine.service `_route`): the reason a buy is refused at
  once when the cached answer says the broker would refuse it; Redis only, no network.
- `TradabilityRefresher`: the worker job (behind the startup gate) that keeps the
  answers fresh for every enabled link with portfolios: first the symbols that matter
  (approved strategies, watchlists of the portfolios' agents, positions, open orders),
  then the rest of the catalog, at most CHUNK symbols per link per tick (the adapters
  respect the brokers' rate limits; a failed read waits for the next tick).
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.brokers.models import BrokerLink
from smtrading.brokers.tradable import REASONS, Tradability, buyable, not_found, unknown
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.portfolios.models import Portfolio, PortfolioStatus

log = logging.getLogger("smtrading.brokers.tradability")

KEY = "smt:broker_tradable:{}"
FRESH_S = 12 * 3600
KEEP_S = 7 * 24 * 3600
CHUNK = 20  # symbols per broker request batch (and per link per worker tick)
TICK_S = 300
MAX_SYMBOLS = 50  # per check_tradable call
TIMEOUT_S = 60.0


def http_client() -> httpx.AsyncClient:
    """The HTTP client of an on-demand check (tests replace it with the fake's)."""
    return httpx.AsyncClient(timeout=20)


def _key(link_id: uuid.UUID) -> str:
    return KEY.format(link_id)


def _text(v: object) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


async def read_cached(redis, link_id: uuid.UUID, symbols: Iterable[str], *,
                      max_age_s: float = FRESH_S, now: float | None = None
                      ) -> dict[str, Tradability]:
    """The cached answers younger than `max_age_s` (missing, stale or unreadable: left
    out). Redis down: {}."""
    symbols = list(dict.fromkeys(symbols))
    if redis is None or not symbols:
        return {}
    try:
        raw = await redis.hmget(_key(link_id), symbols)
    except Exception:
        log.warning("tradability cache unreadable", exc_info=True)
        return {}
    now = time.time() if now is None else now
    out: dict[str, Tradability] = {}
    for sym, value in zip(symbols, raw, strict=True):
        if value is None:
            continue
        try:
            at = float(json.loads(value).get("at"))
        except (ValueError, TypeError, AttributeError):
            continue
        t = Tradability.loads(value)
        if t is not None and now - at < max_age_s:
            out[sym] = t
    return out


async def write_cached(redis, link_id: uuid.UUID, answers: dict[str, Tradability], *,
                       now: float | None = None) -> None:
    """Store the known answers (unknown ones are skipped)."""
    now = time.time() if now is None else now
    mapping = {}
    for sym, t in answers.items():
        if t.code == "unknown":
            continue
        mapping[sym] = json.dumps({**json.loads(t.dumps()), "at": now})
    if redis is None or not mapping:
        return
    try:
        await redis.hset(_key(link_id), mapping=mapping)
        await redis.expire(_key(link_id), KEEP_S)
    except Exception:
        log.warning("tradability cache not written", exc_info=True)


async def cached_at(redis, link_id: uuid.UUID) -> dict[str, float]:
    """symbol -> when its answer was read (the worker's refresh order)."""
    if redis is None:
        return {}
    try:
        raw = await redis.hgetall(_key(link_id))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[_text(k)] = float(json.loads(v).get("at"))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


async def check_link(
    sessionmaker: async_sessionmaker[AsyncSession], redis, link_id: uuid.UUID,
    instruments: list[Instrument], *, http: httpx.AsyncClient | None = None,
) -> dict[str, Tradability]:
    """Ask the link's broker about `instruments` (in chunks of CHUNK), cache the known
    answers. A chunk the broker cannot answer is unknown; never raises for the broker."""
    from smtrading.brokers.service import open_adapter

    out: dict[str, Tradability] = {}
    if not instruments:
        return out
    own = http is None
    client = http_client() if own else http
    try:
        async with sessionmaker() as s, open_adapter(
                s, link_id, client, sessionmaker=sessionmaker) as adapter:
            await s.rollback()  # no transaction across the network calls
            for i in range(0, len(instruments), CHUNK):
                chunk = instruments[i:i + CHUNK]
                try:
                    got = await asyncio.wait_for(adapter.tradability(chunk), TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - the broker did not answer
                    log.warning("tradability of link %s not read: %s", link_id,
                                type(e).__name__)
                    got = {}
                answers = {inst.symbol: got.get(inst.symbol, unknown()) for inst in chunk}
                await write_cached(redis, link_id, answers)
                out.update(answers)
    finally:
        if own:
            await client.aclose()
    return out


def _with_catalog(t: Tradability, inst: Instrument) -> Tradability:
    """A short sale also needs the catalog's permission (risk checks)."""
    if t.short and not inst.shortable:
        return Tradability(t.tradable, t.long_unleveraged, False, t.fractional,
                           t.partial_close, t.code)
    return t


def catalog_tradability(inst: Instrument) -> Tradability:
    """A portfolio without a broker: whatever the catalog lists."""
    return buyable(short=bool(inst.shortable), fractional=inst.lot_size < 1)


async def portfolio_tradability(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], redis,
    portfolio_id: uuid.UUID, symbols: Iterable[str], *, refresh: bool = False,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Tradability]:
    """symbol (canonical) -> its tradability for the portfolio: from the cache, else
    the broker's answer now (`refresh`: the broker's always); without a broker, from the
    catalog. A symbol not in the catalog is `not_in_catalog`."""
    syms = list(dict.fromkeys(canonical_symbol(s) for s in symbols if s and s.strip()))
    portfolio = await session.get(Portfolio, portfolio_id)
    rows = {i.symbol: i for i in await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(syms),
                                 Instrument.active.is_(True)))} if syms else {}
    out: dict[str, Tradability] = {s: not_found("not_in_catalog") for s in syms
                                   if s not in rows}
    known = [s for s in syms if s in rows]
    link_id = portfolio.broker_link_id if portfolio is not None else None
    if link_id is None:
        out.update({s: catalog_tradability(rows[s]) for s in known})
        return {s: out[s] for s in syms}
    cached = {} if refresh else await read_cached(redis, link_id, known)
    missing = [rows[s] for s in known if s not in cached]
    fetched = await check_link(sessionmaker, redis, link_id, missing, http=http)
    for s in known:
        t = cached.get(s) or fetched.get(s) or unknown()
        out[s] = _with_catalog(t, rows[s])
    return {s: out[s] for s in syms}


async def cached_refusal(session: AsyncSession, redis, portfolio: Portfolio,
                         instrument: Instrument) -> str | None:
    """Placement: the reason an opening buy on the linked `portfolio` is refused at once,
    when the cached answer (Redis only, no network) says the broker would refuse it."""
    if portfolio.broker_link_id is None or redis is None:
        return None
    t = (await read_cached(redis, portfolio.broker_link_id, [instrument.symbol])
         ).get(instrument.symbol)
    if t is None or not t.buy_refused:
        return None
    return t.reason or REASONS["not_found"]


# --- the worker job ------------------------------------------------------------------


async def _links(session: AsyncSession) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Enabled link -> its portfolios that are not archived."""
    rows = (await session.execute(
        select(Portfolio.broker_link_id, Portfolio.id)
        .join(BrokerLink, BrokerLink.id == Portfolio.broker_link_id)
        .where(BrokerLink.enabled.is_(True), Portfolio.status != PortfolioStatus.archived)
    )).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for link_id, pid in rows:
        out.setdefault(link_id, []).append(pid)
    return out


async def relevant_symbols(session: AsyncSession, portfolio_ids: list[uuid.UUID]
                           ) -> list[str]:
    """The symbols that matter on these portfolios: approved strategies, the watchlists of
    their agents, positions and open orders."""
    from smtrading.agents.models import AgentPortfolio
    from smtrading.engine.models import OPEN_STATUSES, Order
    from smtrading.positions.models import Position
    from smtrading.signals.service import approved_strategies
    from smtrading.watch.models import Watchlist, WatchlistItem

    ids = set(portfolio_ids)
    out: list[str] = []
    for a in await approved_strategies(session):
        if any(p.id in ids for p in a.portfolios):
            out += a.symbols
    out += await session.scalars(
        select(WatchlistItem.symbol)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .join(AgentPortfolio, AgentPortfolio.agent_id == Watchlist.agent_id)
        .where(AgentPortfolio.portfolio_id.in_(ids)))
    out += await session.scalars(
        select(Position.symbol).where(Position.portfolio_id.in_(ids), Position.qty != 0))
    out += await session.scalars(
        select(Order.symbol).where(Order.portfolio_id.in_(ids),
                                   Order.status.in_(OPEN_STATUSES)))
    return list(dict.fromkeys(canonical_symbol(s) for s in out))


class TradabilityRefresher:
    """Keeps the cached answers fresh (module docstring)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redis,
                 http: httpx.AsyncClient, *, chunk: int = CHUNK, tick_s: float = TICK_S,
                 clock: Callable[[], float] = time.time) -> None:
        self._sm = sessionmaker
        self._redis = redis
        self._http = http
        self._chunk = chunk
        self._tick_s = tick_s
        self._clock = clock

    async def run(self) -> None:
        from smtrading.worker.jobstate import job_tick

        while True:
            try:
                await self.tick()
                await job_tick()
            except Exception:
                log.warning("tradability refresh failed; retrying in %ss", self._tick_s,
                            exc_info=True)
            await asyncio.sleep(self._tick_s)

    async def _due(self, link_id: uuid.UUID, pids: list[uuid.UUID]) -> list[Instrument]:
        now = self._clock()
        async with self._sm() as s:
            first = await relevant_symbols(s, pids)
            catalog = {i.symbol: i for i in await s.scalars(
                select(Instrument).where(Instrument.active.is_(True))
                .order_by(Instrument.symbol))}
        at = await cached_at(self._redis, link_id)
        order = [*first, *(sym for sym in catalog if sym not in set(first))]
        stale = [catalog[sym] for sym in order
                 if sym in catalog and now - at.get(sym, 0.0) >= FRESH_S]
        return stale[: self._chunk]

    async def tick(self) -> int:
        """One pass: at most `chunk` stale symbols per link. Returns how many were read."""
        async with self._sm() as s:
            links = await _links(s)
        done = 0
        for link_id, pids in links.items():
            due = await self._due(link_id, pids)
            if not due:
                continue
            got = await check_link(self._sm, self._redis, link_id, due, http=self._http)
            done += sum(1 for t in got.values() if t.code != "unknown")
        return done
