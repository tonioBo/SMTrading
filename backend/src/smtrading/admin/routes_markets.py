"""Admin API: stream status and tracking, instruments, quotes, market hours."""

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.instruments.market_hours import MARKETS, all_market_status
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata.streaming import STATUS_KEY, track, tracked_symbols, untrack
from smtrading.mcp.dto import instrument_dto, parse_decimal, parse_enum, quote_dto, to_jsonable

router = APIRouter(prefix="/api", tags=["markets"], dependencies=[Depends(require_admin)])

MAX_TRACK_SYMBOLS = 100
MAX_QUOTE_SYMBOLS = 50
MAX_LIST = 200
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


class InstrumentPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    active: bool | None = None
    lot_size: str | int | None = None
    tick_size: str | int | None = None
    shortable: bool | None = None
    provider_symbols: dict[str, str] | None = None


def _dto(i: Instrument) -> dict[str, Any]:
    return {**instrument_dto(i), "provider_symbols": dict(i.provider_symbols or {})}


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
    return {"untracked": syms}


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
    session: DbSession, query: str | None = None, asset_class: str | None = None
) -> list[dict]:
    stmt = select(Instrument).order_by(Instrument.symbol).limit(MAX_LIST)
    ac = parse_enum(AssetClass, asset_class, "asset_class")
    if ac is not None:
        stmt = stmt.where(Instrument.asset_class == ac)
    if query and query.strip():
        like = "%" + re.sub(r"([\\%_])", r"\\\1", query.strip()) + "%"
        stmt = stmt.where(
            or_(Instrument.symbol.ilike(like, escape="\\"), Instrument.name.ilike(like, escape="\\"))
        )
    return [_dto(i) for i in await session.scalars(stmt)]


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
            "is_open": s.is_open,
            "next_open": to_jsonable(s.next_open),
            "next_close": to_jsonable(s.next_close),
        }
        for s in all_market_status(datetime.now(UTC))
    ]
