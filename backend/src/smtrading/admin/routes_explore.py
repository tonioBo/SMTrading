"""Admin API «Mercati» (plan 22): instant search, facets, the instrument sheet, the sector
map and the movers. Read-only (a viewer can read it).

- `GET /api/instruments/search?q=&limit=` → {query, items: [instrument + {score, match}]}:
  the catalog ranked (instruments.explore.search); `match` is the field that matched
  (symbol | name | index | sector | industry).
- `GET /api/instruments/facets` → {sectors: [{sector, count}], indices: [{code, label,
  count}], universes: [{code, label}], sector_list, etf_types}.
- `GET /api/instruments/{symbol}/overview` → {instrument, market_status, positions,
  open_orders, watchlists, strategies, signals}: database only (signals from the cached
  «Segnali» answer, null when there is none).
- `GET /api/instruments/{symbol}/market?timeframe=1d|1h` → {quote, day_change, bars:
  [{ts, close}], indicators: {rsi14, sma20, sma50, atr14, close}, news, next_earnings,
  next_dividend, errors: {part: code}}: each part independent (a failing provider only
  nulls its part), cached MARKET_TTL_S.
- `GET /api/markets/day-changes?symbols=A,B` (at most 100) → {symbol: {last, prev_close,
  change_pct, ts, provider}} for the symbols with a day change (the list page's «Var.»).
- `GET /api/markets/snapshot?universe=SP500|NDX|FTSEMIB|DAX|CAC40|MDAX|CRYPTO|MINE` →
  {universe, label, as_of, total, covered, sectors: [{sector, count, avg_change_pct, up,
  down, members}], gainers, losers}: day changes of the universe from the batch calls
  (MarketDataRouter.get_day_changes), cached SNAPSHOT_TTL_S; computed only when asked.
Numbers are strings.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from redis.exceptions import RedisError
from sqlalchemy import func, select

from smtrading.activity.models import Decision
from smtrading.admin.auth import DbSession, RedisClient, require_access
from smtrading.admin.common import _invalid
from smtrading.admin.routes_signals import cached_signals
from smtrading.agents.models import Agent
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OPEN_STATUSES, Order
from smtrading.instruments import explore
from smtrading.instruments.market_hours import market_status
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.instruments.taxonomy import ETF_TYPES, INDICES, SECTORS
from smtrading.marketdata import indicators
from smtrading.marketdata.indicators_service import fetch_bars
from smtrading.marketdata.types import Timeframe
from smtrading.mcp.dto import instrument_dto, quote_dto, to_jsonable
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.positions.models import Position
from smtrading.watch.models import Watchlist, WatchlistItem

log = logging.getLogger("smtrading.admin.explore")
router = APIRouter(prefix="/api", tags=["markets"], dependencies=[Depends(require_access)])

MARKET_TTL_S = 60
SNAPSHOT_TTL_S = 300
PARTIAL_TTL_S = 60
SNAPSHOT_KEY = "markets:snapshot:v1:"
MARKET_KEY = "markets:sheet:v1:"
DAILY_BARS = 260  # SMA 50 and a long RSI warm-up; the 1d chart shows the last CHART_BARS
CHART_BARS = 120
NEWS_LIMIT = 8
CALENDAR_DAYS = 120


def _router(request: Request):
    r = request.app.state.mcp_runtime.router
    if r is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "dati di mercato non disponibili")
    return r


async def _cache_get(redis, key: str) -> dict | None:
    try:
        raw = await redis.get(key)
    except RedisError:
        log.warning("cache read failed for %s", key, exc_info=True)
        return None
    return json.loads(raw) if raw is not None else None


async def _cache_set(redis, key: str, body: dict, ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(body), ex=ttl)
    except RedisError:
        log.warning("cache write failed for %s", key, exc_info=True)


def _dto(i: Instrument) -> dict[str, Any]:
    return {**instrument_dto(i), "source": str(i.source), "added_by": i.added_by,
            "created_at": to_jsonable(i.created_at)}


# ------------------------------------------------------------------ search and facets


@router.get("/instruments/search")
async def search_instruments(
    session: DbSession, q: str = "", limit: int = Query(30, ge=1, le=explore.MAX_SEARCH),
) -> dict:
    query = q.strip()
    if len(query) > 64:
        raise _invalid("q", "ricerca troppo lunga (max 64 caratteri)")
    hits = await explore.search(session, query, limit)
    return {"query": query, "items": [
        {**_dto(i), "score": score, "match": explore.match_field(i, query)} for i, score in hits
    ]}


@router.get("/instruments/facets")
async def facets(session: DbSession) -> dict:
    active = Instrument.active.is_(True)
    sectors = (await session.execute(
        select(Instrument.sector, func.count()).where(active, Instrument.sector.is_not(None))
        .group_by(Instrument.sector))).all()
    counts = dict(sectors)
    idx = (await session.execute(
        select(func.unnest(Instrument.indices).label("code"), func.count())
        .where(active).group_by("code"))).all()
    by_code = dict(idx)
    return {
        "sectors": [{"sector": s, "count": counts[s]} for s in SECTORS if s in counts],
        "indices": [{"code": c, "label": label, "count": by_code.get(c, 0)}
                    for c, label in INDICES.items()],
        "universes": [{"code": c, "label": label} for c, label in explore.UNIVERSES.items()],
        "sector_list": list(SECTORS),
        "etf_types": list(ETF_TYPES),
    }


# ------------------------------------------------------------------ instrument sheet


async def _instrument(session, symbol: str) -> Instrument:
    instr = await session.get(Instrument, canonical_symbol(symbol))
    if instr is None:
        raise SMTError(ErrorCode.NOT_FOUND, "strumento non trovato", {"symbol": symbol})
    return instr


@router.get("/instruments/{symbol}/overview")
async def overview(symbol: str, session: DbSession, redis: RedisClient) -> dict:
    from smtrading.signals.service import approved_strategies

    instr = await _instrument(session, symbol)
    sym = instr.symbol
    try:
        status = market_status(instr.market, datetime.now(UTC))
    except SMTError:
        status = None
    live = Portfolio.status != PortfolioStatus.archived
    positions = (await session.execute(
        select(Position, Portfolio.name).join(Portfolio, Portfolio.id == Position.portfolio_id)
        .where(Position.symbol == sym, Position.qty != 0, live)
        .order_by(Portfolio.name))).all()
    orders = (await session.execute(
        select(Order, Portfolio.name).join(Portfolio, Portfolio.id == Order.portfolio_id)
        .where(Order.symbol == sym, Order.status.in_(OPEN_STATUSES), live)
        .order_by(Order.created_at.desc()).limit(50))).all()
    watch = (await session.execute(
        select(Agent.name, Watchlist.name).join(Watchlist, Watchlist.agent_id == Agent.id)
        .join(WatchlistItem, WatchlistItem.watchlist_id == Watchlist.id)
        .where(WatchlistItem.symbol == sym).order_by(Agent.name, Watchlist.name))).all()
    strategies = [
        {"ref": a.ref, "name": a.name, "type": a.type, "timeframe": a.timeframe,
         "portfolios": [p.name for p in a.portfolios]}
        for a in await approved_strategies(session) if sym in a.symbols
    ]
    # the latest decisions naming it (proposals, approvals, lessons...), newest first
    decisions = (await session.scalars(
        select(Decision).where(Decision.symbols.contains([sym]))
        .order_by(Decision.id.desc()).limit(5))).all()
    signals = None
    cached = await cached_signals(redis)
    if cached is not None:
        signals = []
        for p in cached.get("portfolios") or []:
            for s in p.get("strategies") or []:
                for row in s.get("symbols") or []:
                    if row.get("symbol") == sym:
                        signals.append({
                            "ref": s.get("ref"), "name": s.get("name"),
                            "portfolio_name": p.get("portfolio_name"),
                            "indicator": row.get("indicator"), "direction": row.get("direction"),
                            "threshold": row.get("threshold"), "distance": row.get("distance"),
                            "triggered_now": row.get("triggered_now"),
                            "position": row.get("position"), "as_of": row.get("as_of"),
                        })
    return to_jsonable({
        "instrument": {**_dto(instr), "provider_symbols": dict(instr.provider_symbols or {})},
        "market_status": status,
        "positions": [
            {"portfolio_id": p.portfolio_id, "portfolio_name": name, "qty": p.qty,
             "avg_price": p.avg_price, "opened_at": p.opened_at}
            for p, name in positions
        ],
        "open_orders": [
            {"id": o.id, "portfolio_id": o.portfolio_id, "portfolio_name": name,
             "side": o.side, "type": o.type, "qty": o.qty, "notional": o.notional,
             "limit_price": o.limit_price, "stop_price": o.stop_price, "role": o.role,
             "status": o.status, "created_at": o.created_at}
            for o, name in orders
        ],
        "watchlists": [{"agent": a, "watchlist": w} for a, w in watch],
        "strategies": strategies,
        "decisions": [
            {"id": d.id, "kind": d.kind, "summary": d.summary, "created_at": d.created_at}
            for d in decisions
        ],
        "signals": signals,
    })


def _last(values: list) -> Any:
    return next((v for v in reversed(values) if v is not None), None)


async def _part(errors: dict, name: str, coro) -> Any:
    try:
        return await coro
    except SMTError as e:
        errors[name] = str(e.code)
    except AttributeError:  # a market data source without this call (tests, fakes)
        errors[name] = str(ErrorCode.PROVIDER_UNAVAILABLE)
    return None


@router.get("/instruments/{symbol}/market")
async def market(
    symbol: str, request: Request, session: DbSession, redis: RedisClient,
    timeframe: str = "1d",
) -> dict:
    if timeframe not in ("1d", "1h"):
        raise _invalid("timeframe", "timeframe deve essere 1d o 1h")
    instr = await _instrument(session, symbol)
    sym = instr.symbol
    key = f"{MARKET_KEY}{sym}:{timeframe}"
    if (hit := await _cache_get(redis, key)) is not None:
        return hit
    r = _router(request)
    errors: dict[str, str] = {}
    today = datetime.now(UTC).date()

    async def quote():
        return (await r.get_quotes([sym]))[sym]

    async def day_change():
        return (await r.get_day_changes([sym])).get(sym)

    async def chart():
        if timeframe == "1d":
            return None  # the daily bars below are the chart
        return await fetch_bars(r, sym, Timeframe.h1, CHART_BARS)

    q, dc, daily, hourly, news, events = await asyncio.gather(
        _part(errors, "quote", quote()),
        _part(errors, "day_change", day_change()),
        _part(errors, "bars", fetch_bars(r, sym, Timeframe.d1, DAILY_BARS)),
        _part(errors, "bars_1h", chart()),
        _part(errors, "news", r.get_news([sym], NEWS_LIMIT)),
        _part(errors, "calendar", r.get_calendar(
            [sym], today, today + timedelta(days=CALENDAR_DAYS))),
    )
    ind = None
    if daily:
        closes = [b.close for b in daily]
        ind = {
            "close": closes[-1], "ts": daily[-1].ts,
            "rsi14": _last(indicators.rsi(closes, 14)),
            "sma20": _last(indicators.sma(closes, 20)),
            "sma50": _last(indicators.sma(closes, 50)),
            "atr14": _last(indicators.atr(daily, 14)),
        }
    shown = hourly if timeframe == "1h" else (daily or [])[-CHART_BARS:]
    if timeframe == "1h" and "bars_1h" in errors:
        errors["bars"] = errors.pop("bars_1h")
    errors.pop("bars_1h", None)

    def first(kind: str):
        return next(({"date": e.date, "data": e.data} for e in events or []
                     if e.kind == kind and e.date >= today), None)

    body = to_jsonable({
        "symbol": sym, "timeframe": timeframe,
        "quote": quote_dto(q) if q is not None else None,
        "day_change": _day_change_dto(dc) if dc is not None else None,
        "bars": [{"ts": b.ts, "close": b.close} for b in shown or []],
        "indicators": ind,
        "news": [{"id": n.id, "ts": n.ts, "headline": n.headline, "url": n.url,
                  "source": n.source} for n in (news or [])[:NEWS_LIMIT]],
        "next_earnings": first("earnings"),
        "next_dividend": first("dividend"),
        "errors": errors,
        "as_of": datetime.now(UTC),
    })
    await _cache_set(redis, key, body, MARKET_TTL_S)
    return body


# ------------------------------------------------------------------ sector map and movers


MAX_DAY_CHANGES = 100


def _day_change_dto(dc) -> dict:
    return {"last": dc.last, "prev_close": dc.prev_close, "change_pct": round(dc.change_pct, 2),
            "ts": dc.ts, "provider": dc.provider}


@router.get("/markets/day-changes")
async def day_changes(request: Request, symbols: str) -> dict:
    syms = list(dict.fromkeys(canonical_symbol(s) for s in symbols.split(",") if s.strip()))
    if not syms or len(syms) > MAX_DAY_CHANGES:
        raise _invalid("symbols", f"indicare da 1 a {MAX_DAY_CHANGES} simboli")
    got = await _router(request).get_day_changes(syms)
    return to_jsonable({s: _day_change_dto(dc) for s, dc in got.items()})


@router.get("/markets/snapshot")
async def snapshot(request: Request, session: DbSession, redis: RedisClient,
                   universe: str = "SP500") -> dict:
    code = universe.strip().upper()
    if code not in explore.UNIVERSES:
        raise _invalid("universe", f"universe deve essere uno tra {list(explore.UNIVERSES)}")
    key = SNAPSHOT_KEY + code
    if (hit := await _cache_get(redis, key)) is not None:
        return hit
    instruments = await explore.universe_instruments(session, code)
    r = _router(request)
    changes = await r.get_day_changes([i.symbol for i in instruments]) if instruments else {}
    rows = explore.members(instruments, changes)
    gainers, losers = explore.movers(rows)
    body = to_jsonable({
        "universe": code, "label": explore.UNIVERSES[code], "as_of": datetime.now(UTC),
        "total": len(instruments), "covered": len(rows),
        "sectors": explore.sector_tiles(rows), "gainers": gainers, "losers": losers,
    })
    # partial (a provider down or rate limited): retried sooner
    ttl = SNAPSHOT_TTL_S if len(rows) >= 0.9 * len(instruments) else PARTIAL_TTL_S
    await _cache_set(redis, key, body, ttl)
    return body
