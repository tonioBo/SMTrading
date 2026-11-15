"""Admin API: stream status and tracking, instruments, quotes, market hours."""

import asyncio
import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.agents.service import write_audit
from smtrading.common.errors import ErrorCode, SMTError
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
from smtrading.marketdata.streaming import (
    STATUS_KEY,
    track,
    tracked_sources,
    tracked_symbols,
    untrack,
)
from smtrading.mcp.dto import instrument_dto, parse_decimal, parse_enum, quote_dto, to_jsonable

router = APIRouter(prefix="/api", tags=["markets"], dependencies=[Depends(require_admin)])

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


def _dto(i: Instrument) -> dict[str, Any]:
    return {
        **instrument_dto(i), "provider_symbols": dict(i.provider_symbols or {}),
        "source": str(i.source), "added_by": i.added_by,
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
    session: DbSession, query: str | None = None, asset_class: str | None = None,
    market: str | None = None, source: str | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_LIST, ge=1, le=MAX_LIST),
) -> dict:
    """A page of the catalog ordered by symbol: {items, total, offset, limit}."""
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
    if query and query.strip():
        like = "%" + re.sub(r"([\\%_])", r"\\\1", query.strip()) + "%"
        conds.append(
            or_(Instrument.symbol.ilike(like, escape="\\"), Instrument.name.ilike(like, escape="\\"))
        )
    total = await session.scalar(select(func.count()).select_from(Instrument).where(*conds))
    rows = await session.scalars(
        select(Instrument).where(*conds).order_by(Instrument.symbol).offset(offset).limit(limit)
    )
    return {"items": [_dto(i) for i in rows], "total": int(total or 0), "offset": offset,
            "limit": limit}


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
        await session.flush()
    return _dto(instrument)


# ------------------------------------------------------------------ quotes, market hours


@router.get("/quotes")
async def get_quotes(request: Request, symbols: str) -> dict:
    """`symbols=A,B` (at most 50): one batch via the router, then per-symbol on failure."""
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
    return {s: found[s] if isinstance(found[s], dict) else quote_dto(found[s]) for s in found}


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
