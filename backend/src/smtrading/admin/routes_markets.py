"""Admin API: stream status and tracking, instruments, quotes, market hours."""

import asyncio
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import false, func, or_, select
from sqlalchemy.exc import IntegrityError

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_access
from smtrading.admin.common import _invalid, _transaction
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments import explore
from smtrading.instruments.closures import (
    MarketClosure,
    add_closure,
    delete_closure,
    refresh_closures,
)
from smtrading.instruments.discovery import DAILY_LIMIT, auto_add_enabled, set_auto_add
from smtrading.instruments.market_hours import MARKETS, all_market_status
from smtrading.instruments.models import AssetClass, Instrument, InstrumentSource
from smtrading.instruments.service import canonical_symbol
from smtrading.instruments.taxonomy import INDICES, SECTORS
from smtrading.marketdata.extended import extended_hours
from smtrading.marketdata.streaming import (
    STATUS_KEY,
    track,
    tracked_sources,
    tracked_symbols,
    untrack,
)
from smtrading.mcp.dto import instrument_dto, parse_decimal, parse_enum, quote_dto, to_jsonable

router = APIRouter(prefix="/api", tags=["markets"], dependencies=[Depends(require_access)])

MAX_TRACK_SYMBOLS = 100
MAX_QUOTE_SYMBOLS = 50
MAX_LIST = 500
DEFAULT_LIST = 100
MAX_NAME_LEN = 256
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-_/=^]{0,63}$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


class SymbolsIn(BaseModel):
    symbols: list[str]


class InstrumentCreateIn(BaseModel):
    symbol: str
    name: str
    asset_class: str
    market: str
    currency: str
    lot_size: str | int = 1
    tick_size: str | int = "0.01"
    shortable: bool = False
    provider_symbols: dict[str, str] = {}


class ClosureIn(BaseModel):
    market: str
    day: date
    kind: str
    close_time: time | None = None
    note: str = ""


class AutoAddIn(BaseModel):
    on: bool


class InstrumentPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    active: bool | None = None
    lot_size: str | int | None = None
    tick_size: str | int | None = None
    shortable: bool | None = None
    provider_symbols: dict[str, str] | None = None
    sector: str | None = None
    industry: str | None = None
    indices: list[str] | None = None


def _dto(i: Instrument) -> dict[str, Any]:
    return {
        **instrument_dto(i), "provider_symbols": dict(i.provider_symbols or {}),
        "source": str(i.source), "added_by": i.added_by,
        "created_at": to_jsonable(i.created_at),
    }


# ------------------------------------------------------------------ stream and tracking


@router.get("/marketdata/stream-status")
async def stream_status(redis: RedisClient) -> dict:
    """The worker's last published status; `available` is false when it is stale or absent."""
    raw = await redis.get(STATUS_KEY)
    status = json.loads(raw) if raw else {"streams": {}, "polling": []}
    return {
        **status,
        "available": raw is not None,
        "tracked": sorted(await tracked_symbols(redis)),
        # symbol -> who follows it: orders, positions, alerts, watchlists (agents), admin
        "tracked_sources": await tracked_sources(redis),
    }


async def _clean_symbols(body: SymbolsIn) -> list[str]:
    syms = list(dict.fromkeys(canonical_symbol(s) for s in body.symbols))
    if not syms or len(syms) > MAX_TRACK_SYMBOLS:
        raise _invalid("symbols", f"indicare da 1 a {MAX_TRACK_SYMBOLS} simboli")
    if any(not _SYMBOL.match(s) for s in syms):
        raise _invalid("symbols", "simbolo non valido")
    return syms


@router.post("/marketdata/track")
async def track_symbols(body: SymbolsIn, session: DbSession, redis: RedisClient) -> dict:
    syms = await _clean_symbols(body)
    known = set(await session.scalars(select(Instrument.symbol).where(Instrument.symbol.in_(syms))))
    unknown = [s for s in syms if s not in known]
    if unknown:
        raise SMTError(ErrorCode.SYMBOL_NOT_FOUND, "simboli sconosciuti", {"symbols": unknown})
    await track(redis, syms, source="admin")
    return {"tracked": syms}


@router.post("/marketdata/untrack")
async def untrack_symbols(body: SymbolsIn, redis: RedisClient) -> dict:
    syms = await _clean_symbols(body)
    await untrack(redis, syms, source="admin")
    sources = await tracked_sources(redis)
    # the admin stops following; other sources (agents' watchlists, orders...) may still
    return {"untracked": syms, "still_tracked": {s: sources[s] for s in syms if s in sources}}


# ------------------------------------------------------------------ instruments


def _text(value: str | None, field: str, maximum: int) -> str:
    text = (value or "").strip()
    if not text or len(text) > maximum:
        raise _invalid(field, f"{field}: da 1 a {maximum} caratteri")
    return text


def _positive(value: Any, field: str) -> Decimal:
    parsed = parse_decimal(value, field)
    if parsed is None or parsed <= 0:
        raise _invalid(field, f"{field} deve essere positivo")
    return parsed


def _provider_symbols(value: dict[str, str]) -> dict[str, str]:
    if any(not k or not v for k, v in value.items()):
        raise _invalid("provider_symbols", "nomi e simboli dei provider non possono essere vuoti")
    return dict(value)


@router.get("/instruments")
async def list_instruments(
    request: Request, session: DbSession, redis: RedisClient,
    query: str | None = None, asset_class: str | None = None,
    market: str | None = None, source: str | None = None, sector: str | None = None,
    index: str | None = None, quick: str | None = None,
    sort: str = "symbol", dir: str = "asc",
    offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_LIST, ge=1, le=MAX_LIST),
) -> dict:
    """A page of the catalog: {items, total, offset, limit}. Filters: query (symbol or
    name contains), asset_class, market, source, sector (one of taxonomy.SECTORS, or
    "Senza settore"), index (an index code), quick (portfolio: open positions, watchlist:
    in an agent's watchlist, strategy: used by an approved strategy, signal: entry signal
    now in «Segnali», recent: added in the last 7 days or on demand). Sort: symbol | name
    | sector | market | asset_class | created_at, dir asc | desc (then by symbol)."""
    conds = []
    ac = parse_enum(AssetClass, asset_class, "asset_class")
    if ac is not None:
        conds.append(Instrument.asset_class == ac)
    if market:
        if market not in MARKETS:
            raise _invalid("market", f"market deve essere uno tra {list(MARKETS)}")
        conds.append(Instrument.market == market)
    src = parse_enum(InstrumentSource, source, "source")
    if src is not None:
        conds.append(Instrument.source == src)
    if sector:
        if sector == explore.UNSECTORED:
            conds.append(Instrument.sector.is_(None))
        elif sector in SECTORS:
            conds.append(Instrument.sector == sector)
        else:
            raise _invalid("sector", "settore sconosciuto")
    if index:
        if index not in INDICES:
            raise _invalid("index", f"index deve essere uno tra {list(INDICES)}")
        conds.append(Instrument.indices.contains([index]))
    if quick:
        conds.append(await _quick(request, session, redis, quick))
    if query and query.strip():
        like = "%" + explore.escape_like(query.strip()) + "%"
        conds.append(
            or_(Instrument.symbol.ilike(like, escape="\\"),
                Instrument.name.ilike(like, escape="\\"))
        )
    col = explore.SORTS.get(sort)
    if col is None or dir not in ("asc", "desc"):
        raise _invalid("sort", f"sort deve essere uno tra {list(explore.SORTS)}, dir asc o desc")
    order = col.desc().nulls_last() if dir == "desc" else col.asc().nulls_last()
    total = await session.scalar(select(func.count()).select_from(Instrument).where(*conds))
    rows = await session.scalars(
        select(Instrument).where(*conds).order_by(order, Instrument.symbol)
        .offset(offset).limit(limit)
    )
    return {"items": [_dto(i) for i in rows], "total": int(total or 0), "offset": offset,
            "limit": limit}


async def _quick(request: Request, session, redis, quick: str):
    if quick == "portfolio":
        syms = await explore.held_symbols(session)
    elif quick == "watchlist":
        syms = await explore.watchlist_symbols(session)
    elif quick == "strategy":
        syms = await explore.strategy_symbols(session)
    elif quick == "signal":
        from smtrading.admin.routes_signals import signals_body

        syms = explore.signal_symbols(await signals_body(request, session, redis))
    elif quick == "recent":
        return explore.recent_condition()
    else:
        raise _invalid("quick", f"quick deve essere uno tra {list(explore.QUICK_FILTERS)}")
    return Instrument.symbol.in_(sorted(syms)) if syms else false()


@router.get("/instruments/auto-add")
async def get_auto_add(session: DbSession) -> dict:
    """Automatic on-demand instruments for agents (instruments.discovery)."""
    return {"on": await auto_add_enabled(session), "daily_limit": DAILY_LIMIT}


@router.put("/instruments/auto-add")
async def put_auto_add(body: AutoAddIn, session: DbSession, redis: RedisClient) -> dict:
    async with _transaction(session, redis):
        await set_auto_add(session, body.on)
    return {"on": body.on, "daily_limit": DAILY_LIMIT}


@router.post("/instruments")
async def create_instrument(body: InstrumentCreateIn, session: DbSession, redis: RedisClient) -> dict:
    symbol = canonical_symbol(body.symbol)
    if not _SYMBOL.match(symbol):
        raise _invalid("symbol", "simbolo non valido")
    asset_class = parse_enum(AssetClass, body.asset_class, "asset_class")
    if body.market not in MARKETS:
        raise _invalid("market", f"market deve essere uno tra {list(MARKETS)}")
    currency = body.currency.strip()
    if not _CURRENCY.match(currency):
        raise _invalid("currency", "la valuta deve avere 3 lettere")
    instrument = Instrument(
        symbol=symbol, name=_text(body.name, "name", MAX_NAME_LEN), asset_class=asset_class,
        market=body.market, currency=currency.upper(),
        lot_size=_positive(body.lot_size, "lot_size"),
        tick_size=_positive(body.tick_size, "tick_size"),
        shortable=body.shortable, active=True,
        provider_symbols=_provider_symbols(body.provider_symbols),
    )
    try:
        async with _transaction(session, redis):
            if await session.get(Instrument, symbol) is not None:
                raise _invalid("symbol", "strumento già esistente")
            session.add(instrument)
            await session.flush()
    except IntegrityError:  # a concurrent creation won the race
        raise _invalid("symbol", "strumento già esistente") from None
    return _dto(instrument)


@router.patch("/instruments/{symbol}")
async def patch_instrument(
    symbol: str, body: InstrumentPatchIn, session: DbSession, redis: RedisClient
) -> dict:
    instrument = await session.get(Instrument, canonical_symbol(symbol))
    if instrument is None:
        raise SMTError(ErrorCode.NOT_FOUND, "strumento non trovato", {"symbol": symbol})
    fields = body.model_fields_set
    for f in fields:
        if getattr(body, f) is None:
            raise _invalid(f, f"{f} non può essere nullo")
    async with _transaction(session, redis):
        if "name" in fields:
            instrument.name = _text(body.name, "name", MAX_NAME_LEN)
        if "active" in fields:
            instrument.active = bool(body.active)
        if "lot_size" in fields:
            instrument.lot_size = _positive(body.lot_size, "lot_size")
        if "tick_size" in fields:
            instrument.tick_size = _positive(body.tick_size, "tick_size")
        if "shortable" in fields:
            instrument.shortable = bool(body.shortable)
        if "provider_symbols" in fields:
            instrument.provider_symbols = _provider_symbols(body.provider_symbols or {})
        if "sector" in fields:
            if body.sector not in SECTORS:
                raise _invalid("sector", "settore sconosciuto")
            instrument.sector = body.sector
        if "industry" in fields:
            instrument.industry = _text(body.industry, "industry", 128)
        if "indices" in fields:
            codes = list(dict.fromkeys(body.indices or []))
            if any(c not in INDICES for c in codes):
                raise _invalid("indices", f"indici ammessi: {list(INDICES)}")
            instrument.indices = codes
        await session.flush()
    return _dto(instrument)


# ------------------------------------------------------------------ quotes, market hours


@router.get("/quotes")
async def get_quotes(request: Request, session: DbSession, symbols: str) -> dict:
    """`symbols=A,B` (at most 50): one batch via the router, then per-symbol on failure.
    A quoted US equity also has `extended` (marketdata.extended: the latest pre-market /
    after-hours price while the regular session is closed, else null; information only)."""
    syms = list(dict.fromkeys(canonical_symbol(s) for s in symbols.split(",") if s.strip()))
    if not syms or len(syms) > MAX_QUOTE_SYMBOLS:
        raise _invalid("symbols", f"indicare da 1 a {MAX_QUOTE_SYMBOLS} simboli")
    router_ = request.app.state.mcp_runtime.router
    if router_ is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "dati di mercato non disponibili")
    try:
        found = dict(await router_.get_quotes(syms))
    except SMTError:
        found = None  # all-or-error batch: isolate the failing symbols

    async def one(sym: str) -> Any:
        try:
            return (await router_.get_quotes([sym]))[sym]
        except SMTError as e:
            return {"error": str(e.code)}

    if found is None:
        found = dict(zip(syms, await asyncio.gather(*(one(s) for s in syms)), strict=True))
    quoted = [s for s in found if not isinstance(found[s], dict)]
    instrs = list(await session.scalars(
        select(Instrument).where(Instrument.symbol.in_(quoted)))) if quoted else []
    ext = await extended_hours(router_, instrs)
    out: dict[str, Any] = {}
    for s, q in found.items():
        if isinstance(q, dict):
            out[s] = q
        else:
            out[s] = quote_dto(q)
            if s in ext:
                out[s]["extended"] = to_jsonable(ext[s])
    return out


@router.get("/markets/status")
async def markets_status() -> list[dict]:
    return [
        {
            "market": s.market,
            "tradable": s.market in MARKETS,  # LSE: hours only
            "is_open": s.is_open,
            "next_open": to_jsonable(s.next_open),
            "next_close": to_jsonable(s.next_close),
        }
        for s in all_market_status(datetime.now(UTC))
    ]


# ------------------------------------------------------------------ manual closures


def _closure_dto(c: MarketClosure) -> dict[str, Any]:
    return {
        "id": c.id, "market": c.market, "day": c.day.isoformat(), "kind": c.kind,
        "close_time": c.close_time.strftime("%H:%M") if c.close_time is not None else None,
        "note": c.note, "created_by": c.created_by, "created_at": to_jsonable(c.created_at),
    }


@router.get("/market-closures")
async def list_closures(
    session: DbSession, from_: Annotated[date | None, Query(alias="from")] = None
) -> list[dict]:
    """The manual closures from `from` (default: today, UTC) on, by day then market."""
    since = from_ or datetime.now(UTC).date()
    rows = await session.scalars(select(MarketClosure).where(MarketClosure.day >= since)
                                 .order_by(MarketClosure.day, MarketClosure.market))
    return [_closure_dto(c) for c in rows]


@router.post("/market-closures")
async def create_closure(body: ClosureIn, session: DbSession, redis: RedisClient,
                         admin: CurrentAdmin) -> dict:
    try:
        async with _transaction(session, redis):
            closure = await add_closure(
                session, market=body.market.strip().upper(), day=body.day, kind=body.kind,
                close_time=body.close_time, note=body.note, by=f"admin:{admin.email}")
            dto = _closure_dto(closure)
            await write_audit(
                session, agent_id=None, method="admin:market_closure.add",
                params={"closure_id": closure.id, "market": closure.market,
                        "day": dto["day"], "kind": closure.kind,
                        "close_time": dto["close_time"], "note": closure.note,
                        "by": admin.email},
                outcome="ok", latency_ms=0,
            )
    except IntegrityError:  # a concurrent insert of the same market and day won the race
        raise _invalid("day", "esiste già una chiusura per questo mercato e giorno") from None
    await refresh_closures(session)  # this process applies it at once
    return dto


@router.delete("/market-closures/{closure_id}")
async def remove_closure(closure_id: int, session: DbSession, redis: RedisClient,
                         admin: CurrentAdmin) -> dict:
    async with _transaction(session, redis):
        await delete_closure(session, closure_id)
        await write_audit(
            session, agent_id=None, method="admin:market_closure.delete",
            params={"closure_id": closure_id, "by": admin.email}, outcome="ok", latency_ms=0,
        )
    await refresh_closures(session)
    return {"deleted": closure_id}
