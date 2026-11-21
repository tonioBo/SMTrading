"""Admin API «Mercati v2» (plan 30). Portal only: none of it is an MCP tool or resource,
nothing is added to the trading catalog, favourites and personal alerts are apart from
the agents' watchlists and alerts.

- `GET /api/markets/overview` → {as_of, ttl_s, stale, items: [{code, symbol, label, group,
  unit, decimals, note, currency, market, value, prev_close, change_pct, ts, is_open,
  next_open, next_close, spark: [[epoch_s, value]]}]}: «Mercati oggi» (marketsportal.
  overview: Yahoo index symbols, one batch, cached 60 s / 5 min when markets are closed).
- `GET /api/markets/overview/{code}/chart?range=1d|5d|1mo|1y` → {code, range, label,
  points: [[epoch_s, value]], error}.
- `GET /api/markets/favorites` → {items: [instrument + {position}]} of the signed-in
  user; `PUT /api/markets/favorites/{symbol}` adds a catalog instrument (at the end),
  `DELETE` removes it, `PUT /api/markets/favorites` {symbols} sets the order. Every user
  (viewers too: they are personal), at most MAX_FAVORITES.
- `GET /api/markets/screener?preset=&universe=ALL|<universe>&limit=` → {preset, label,
  presets: [{code, label}], universe, items: [row], computed, total, as_of}: the
  precomputed rows (marketsportal.screener) matching the preset.
- `GET /api/instruments/{symbol}/fundamentals` → {symbol, market_cap, pe_trailing,
  pe_forward, dividend_yield, low_52w, high_52w, beta, next_earnings,
  earnings_estimate, ex_dividend, dividend_date, currency, source} (cached 12 h; nulls
  when not available).
- `GET /api/markets/calendar` → {from, to, today, events: [{date, kind: earnings |
  ex_dividend | macro, symbol, name, sector, held, portfolios, estimate, label}],
  covered, total, macro: {as_of, sources, note}}.
- `GET /api/markets/news?symbol=&sector=&portfolios=true` → {symbols, items: [{id, ts,
  headline, summary, url, source, symbols}], error}: the day's headlines through the
  market data router (its relevance filter), cached NEWS_TTL_S.
- `GET|POST /api/markets/price-alerts`, `PATCH|DELETE /api/markets/price-alerts/{id}`:
  the owner's personal price alerts (admins only; sent once to Telegram by the worker).
Numbers are strings.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select

from smtrading.admin.auth import CurrentAdmin, CurrentUser, DbSession, RedisClient, require_user
from smtrading.admin.common import _invalid
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments import explore
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketsportal import calendar as cal
from smtrading.marketsportal.fundamentals import FundamentalsService
from smtrading.marketsportal.models import (
    ABOVE,
    ALERT_ACTIVE,
    BELOW,
    SOURCE_CATALOG,
    SOURCE_OVERVIEW,
    MarketScreenRow,
    PortalFavorite,
    PortalPriceAlert,
)
from smtrading.marketsportal.overview import BY_CODE, CHART_RANGES, OverviewService
from smtrading.marketsportal.screener import PRESETS, row_dto
from smtrading.marketsportal.yahoo import PortalYahoo
from smtrading.mcp.dto import instrument_dto, parse_decimal, to_jsonable

log = logging.getLogger("smtrading.admin.markets_v2")
router = APIRouter(prefix="/api", tags=["markets"], dependencies=[Depends(require_user)])

MAX_FAVORITES = 100
MAX_ALERTS = 200
MAX_NOTE = 200
NEWS_TTL_S = 600
NEWS_KEY = "portal:news:v1:"
NEWS_LIMIT = 30
NEWS_SYMBOLS = 10
NEWS_RECENT = timedelta(hours=36)
CALENDAR_KEY = "portal:calendar:v1:"
CALENDAR_TTL_S = 3600
MARKET_NEWS = ("SPY", "QQQ", "DIA", "IWM", "ETFMIB.MI", "EXS1.DE")
SCREEN_UNIVERSES = {"ALL": "Tutto il catalogo", **explore.UNIVERSES}


@dataclass
class Services:
    yahoo: PortalYahoo
    overview: OverviewService
    fundamentals: FundamentalsService


def services(request: Request) -> Services:
    """The app's «Mercati v2» services, built on first use (`app.state.markets_v2_raw`:
    a fake Yahoo adapter in tests)."""
    st = request.app.state
    svc = getattr(st, "markets_v2", None)
    if svc is None:
        yahoo = PortalYahoo(st.redis, st.sessionmaker, raw=getattr(st, "markets_v2_raw", None))
        svc = Services(yahoo, OverviewService(st.redis, yahoo),
                       FundamentalsService(st.redis, yahoo))
        st.markets_v2 = svc
    return svc


def _router(request: Request):
    return request.app.state.mcp_runtime.router


async def _cache_get(redis, key: str) -> Any:
    try:
        raw = await redis.get(key)
    except RedisError:
        return None
    return json.loads(raw) if raw is not None else None


async def _cache_set(redis, key: str, body: Any, ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(body), ex=ttl)
    except RedisError:
        log.warning("cache write failed for %s", key, exc_info=True)


async def _instrument(session, symbol: str) -> Instrument:
    instr = await session.get(Instrument, canonical_symbol(symbol))
    if instr is None:
        raise SMTError(ErrorCode.NOT_FOUND, "strumento non trovato", {"symbol": symbol})
    return instr


# ------------------------------------------------------------------ «Mercati oggi»


@router.get("/markets/overview")
async def overview(request: Request) -> dict:
    return await services(request).overview.snapshot()


@router.get("/markets/overview/{code}/chart")
async def overview_chart(code: str, request: Request, range: str = "1d") -> dict:
    if code not in BY_CODE:
        raise SMTError(ErrorCode.NOT_FOUND, "indice sconosciuto", {"code": code})
    if range not in CHART_RANGES:
        raise _invalid("range", f"range deve essere uno tra {list(CHART_RANGES)}")
    return await services(request).overview.chart(code, range)


# ------------------------------------------------------------------ favourites


class FavoriteOrderIn(BaseModel):
    symbols: list[str]


def _instrument_dto(i: Instrument) -> dict:
    return {**instrument_dto(i), "source": str(i.source)}


async def _favorites(session, user_id: uuid.UUID) -> dict:
    rows = (await session.execute(
        select(PortalFavorite, Instrument)
        .join(Instrument, Instrument.symbol == PortalFavorite.symbol)
        .where(PortalFavorite.admin_user_id == user_id)
        .order_by(PortalFavorite.position, PortalFavorite.created_at,
                  PortalFavorite.symbol))).all()
    return to_jsonable({"items": [{**_instrument_dto(i), "position": f.position}
                                  for f, i in rows]})


@router.get("/markets/favorites")
async def list_favorites(session: DbSession, user: CurrentUser) -> dict:
    return await _favorites(session, user.id)


@router.put("/markets/favorites/{symbol}")
async def add_favorite(symbol: str, session: DbSession, user: CurrentUser) -> dict:
    instr = await _instrument(session, symbol)
    existing = await session.get(PortalFavorite, (user.id, instr.symbol))
    if existing is None:
        count = await session.scalar(select(func.count()).select_from(PortalFavorite)
                                     .where(PortalFavorite.admin_user_id == user.id))
        if (count or 0) >= MAX_FAVORITES:
            raise _invalid("symbol", f"al massimo {MAX_FAVORITES} preferiti")
        last = await session.scalar(select(func.max(PortalFavorite.position))
                                    .where(PortalFavorite.admin_user_id == user.id))
        session.add(PortalFavorite(admin_user_id=user.id, symbol=instr.symbol,
                                   position=(last if last is not None else -1) + 1))
        await session.commit()
    return await _favorites(session, user.id)


@router.delete("/markets/favorites/{symbol}")
async def remove_favorite(symbol: str, session: DbSession, user: CurrentUser) -> dict:
    await session.execute(delete(PortalFavorite).where(
        PortalFavorite.admin_user_id == user.id,
        PortalFavorite.symbol == canonical_symbol(symbol)))
    await session.commit()
    return await _favorites(session, user.id)


@router.put("/markets/favorites")
async def order_favorites(body: FavoriteOrderIn, session: DbSession, user: CurrentUser) -> dict:
    order = list(dict.fromkeys(canonical_symbol(s) for s in body.symbols))
    rows = {f.symbol: f for f in await session.scalars(
        select(PortalFavorite).where(PortalFavorite.admin_user_id == user.id))}
    if set(order) != set(rows):
        raise _invalid("symbols", "indicare tutti e soli i preferiti")
    for n, s in enumerate(order):
        rows[s].position = n
    await session.commit()
    return await _favorites(session, user.id)


# ------------------------------------------------------------------ screener


@router.get("/markets/screener")
async def screener(session: DbSession, preset: str = "rsi_oversold", universe: str = "ALL",
                   limit: int = Query(200, ge=1, le=1000)) -> dict:
    if preset not in PRESETS:
        raise _invalid("preset", f"preset deve essere uno tra {list(PRESETS)}")
    code = universe.strip().upper()
    if code not in SCREEN_UNIVERSES:
        raise _invalid("universe", f"universe deve essere uno tra {list(SCREEN_UNIVERSES)}")
    if code == "ALL":
        instruments = list(await session.scalars(
            select(Instrument).where(Instrument.active.is_(True))))
    else:
        instruments = await explore.universe_instruments(session, code)
    by_sym = {i.symbol: i for i in instruments}
    rows = list(await session.scalars(
        select(MarketScreenRow).where(MarketScreenRow.symbol.in_(list(by_sym)))))
    label, match = PRESETS[preset]
    hits = [row_dto(r, by_sym[r.symbol]) for r in rows if match(r)]
    hits.sort(key=lambda h: h["symbol"])
    as_of = max((r.computed_at for r in rows), default=None)
    return to_jsonable({
        "preset": preset, "label": label,
        "presets": [{"code": c, "label": lbl} for c, (lbl, _) in PRESETS.items()],
        "universe": code, "universes": [{"code": c, "label": lbl}
                                        for c, lbl in SCREEN_UNIVERSES.items()],
        "items": hits[:limit], "matched": len(hits),
        "computed": len(rows), "total": len(instruments), "as_of": as_of,
    })


# ------------------------------------------------------------------ fundamentals


@router.get("/instruments/{symbol}/fundamentals")
async def fundamentals(symbol: str, request: Request, session: DbSession) -> dict:
    instr = await _instrument(session, symbol)
    body = await services(request).fundamentals.one(instr, _router(request))
    return {"symbol": instr.symbol, **body}


# ------------------------------------------------------------------ calendar


@router.get("/markets/calendar")
async def calendar(request: Request, session: DbSession, redis: RedisClient) -> dict:
    today = datetime.now(UTC).date()
    key = CALENDAR_KEY + today.isoformat()
    holdings = await cal.held(session)
    hit = await _cache_get(redis, key)
    if hit is None:
        instruments = await cal.equities(session)
        figures = await services(request).fundamentals.many(instruments)
        hit = cal.assemble(instruments, figures, {}, today)
        ttl = CALENDAR_TTL_S if hit["covered"] >= 0.9 * max(hit["total"], 1) else 300
        await _cache_set(redis, key, hit, ttl)
    # the holdings change during the day: applied on every request
    for e in hit["events"]:
        if e.get("symbol"):
            e["portfolios"] = holdings.get(e["symbol"], [])
            e["held"] = bool(e["portfolios"])
    hit["events"].sort(key=lambda e: (e["date"], e["kind"] != "macro", not e["held"],
                                      e["symbol"] or ""))
    return hit


# ------------------------------------------------------------------ news


@router.get("/markets/news")
async def news(request: Request, session: DbSession, redis: RedisClient,
               symbol: str | None = None, sector: str | None = None,
               portfolios: bool = False) -> dict:
    if symbol:
        syms = [(await _instrument(session, symbol)).symbol]
    elif portfolios:
        syms = sorted(await explore.held_symbols(session))[:NEWS_SYMBOLS]
    elif sector:
        held = await explore.held_symbols(session)
        rows = list(await session.scalars(
            select(Instrument.symbol).where(Instrument.active.is_(True),
                                            Instrument.sector == sector)
            .order_by(func.cardinality(Instrument.indices).desc(), Instrument.symbol)))
        syms = ([s for s in rows if s in held] + [s for s in rows if s not in held])[
            :NEWS_SYMBOLS]
    else:
        known = set(await session.scalars(
            select(Instrument.symbol).where(Instrument.symbol.in_(MARKET_NEWS),
                                            Instrument.active.is_(True))))
        syms = [s for s in MARKET_NEWS if s in known]
    if not syms:
        return {"symbols": [], "items": [], "error": None}
    key = NEWS_KEY + ",".join(sorted(syms))
    if (hit := await _cache_get(redis, key)) is not None:
        return hit
    error = None
    items: list = []
    r = _router(request)
    try:
        items = list(await r.get_news(syms, NEWS_LIMIT)) if r is not None else []
    except SMTError as e:
        error = str(e.code)
    except AttributeError:
        error = str(ErrorCode.PROVIDER_UNAVAILABLE)
    since = datetime.now(UTC) - NEWS_RECENT
    recent = [n for n in items if n.ts >= since] or items[:10]
    body = to_jsonable({"symbols": syms, "error": error, "items": [
        {"id": n.id, "ts": n.ts, "headline": n.headline, "summary": n.summary, "url": n.url,
         "source": n.source, "symbols": list(n.symbols)} for n in recent]})
    await _cache_set(redis, key, body, NEWS_TTL_S if error is None else 60)
    return body


# ------------------------------------------------------------------ personal alerts


class AlertIn(BaseModel):
    symbol: str
    source: str = SOURCE_CATALOG
    direction: str
    threshold: str | int | float
    note: str = ""


class AlertPatchIn(BaseModel):
    direction: str | None = None
    threshold: str | int | float | None = None
    note: str | None = None
    rearm: bool | None = None  # a triggered alert becomes active again


def _alert_dto(a: PortalPriceAlert) -> dict:
    name = BY_CODE[a.symbol].label if a.source == SOURCE_OVERVIEW and a.symbol in BY_CODE \
        else a.symbol
    return to_jsonable({
        "id": a.id, "symbol": a.symbol, "label": name, "source": a.source,
        "direction": a.direction, "threshold": a.threshold, "note": a.note,
        "status": a.status, "created_at": a.created_at, "triggered_at": a.triggered_at,
        "triggered_price": a.triggered_price,
    })


def _direction(v: str | None) -> str:
    if v not in (ABOVE, BELOW):
        raise _invalid("direction", "direction deve essere above o below")
    return v


def _threshold(v: Any) -> Decimal:
    d = parse_decimal(str(v), "threshold")
    if d is None or d <= 0:
        raise _invalid("threshold", "la soglia deve essere un numero positivo")
    return d


def _note(v: str | None) -> str:
    text = (v or "").strip()
    if len(text) > MAX_NOTE:
        raise _invalid("note", f"nota: al massimo {MAX_NOTE} caratteri")
    return text


@router.get("/markets/price-alerts")
async def list_alerts(session: DbSession, admin: CurrentAdmin) -> list[dict]:
    rows = await session.scalars(select(PortalPriceAlert).order_by(
        PortalPriceAlert.status, PortalPriceAlert.created_at.desc()))
    return [_alert_dto(a) for a in rows]


@router.post("/markets/price-alerts")
async def create_alert(body: AlertIn, session: DbSession, admin: CurrentAdmin) -> dict:
    if body.source == SOURCE_OVERVIEW:
        if body.symbol not in BY_CODE:
            raise _invalid("symbol", "indice sconosciuto")
        symbol = body.symbol
    elif body.source == SOURCE_CATALOG:
        symbol = (await _instrument(session, body.symbol)).symbol
    else:
        raise _invalid("source", "source deve essere catalog o overview")
    count = await session.scalar(select(func.count()).select_from(PortalPriceAlert))
    if (count or 0) >= MAX_ALERTS:
        raise _invalid("symbol", f"al massimo {MAX_ALERTS} avvisi")
    a = PortalPriceAlert(symbol=symbol, source=body.source, direction=_direction(body.direction),
                         threshold=_threshold(body.threshold), note=_note(body.note),
                         status=ALERT_ACTIVE, created_by=admin.id)
    session.add(a)
    await session.commit()
    await session.refresh(a)
    return _alert_dto(a)


async def _alert(session, alert_id: int) -> PortalPriceAlert:
    a = await session.get(PortalPriceAlert, alert_id, with_for_update=True)
    if a is None:
        raise SMTError(ErrorCode.NOT_FOUND, "avviso non trovato", {"id": alert_id})
    return a


@router.patch("/markets/price-alerts/{alert_id}")
async def patch_alert(alert_id: int, body: AlertPatchIn, session: DbSession,
                      admin: CurrentAdmin) -> dict:
    a = await _alert(session, alert_id)
    fields = body.model_fields_set
    if "direction" in fields:
        a.direction = _direction(body.direction)
    if "threshold" in fields:
        a.threshold = _threshold(body.threshold)
    if "note" in fields:
        a.note = _note(body.note)
    if body.rearm or fields & {"direction", "threshold"}:
        # a changed condition is a new alert: it may fire again
        a.status, a.triggered_at, a.triggered_price = ALERT_ACTIVE, None, None
    await session.commit()
    await session.refresh(a)
    return _alert_dto(a)


@router.delete("/markets/price-alerts/{alert_id}")
async def delete_alert(alert_id: int, session: DbSession, admin: CurrentAdmin) -> dict:
    a = await _alert(session, alert_id)
    await session.delete(a)
    await session.commit()
    return {"deleted": alert_id}
