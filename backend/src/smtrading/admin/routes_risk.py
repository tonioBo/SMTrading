"""Admin API: risk profiles, interest rates, app settings and the kill switch."""

import re
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.admin.schemas import (
    AppSettingsIn,
    InterestRateIn,
    KillSwitchIn,
    RiskProfileCreateIn,
    RiskProfilePatchIn,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.fingerprints import name_warning
from smtrading.common.money import decimal_to_str
from smtrading.engine.service import set_kill_switch_and_cancel
from smtrading.instruments.models import AssetClass
from smtrading.mcp.dto import parse_decimal, parse_enum, to_jsonable
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.risk.models import AppSetting, InterestKind, InterestRate, RiskProfile
from smtrading.risk.profiles import DEFAULT_RISK_PROFILE_NAME
from smtrading.risk.settings import (
    DEFAULT_SPREAD_BPS,
    DEFAULT_STALE_QUOTE_S,
    is_kill_switch_on,
)

router = APIRouter(prefix="/api", tags=["risk"], dependencies=[Depends(require_admin)])

MAX_NAME_LEN = 128
MAX_DESCRIPTION_LEN = 500
MAX_RATE_PCT = Decimal(1000)
MAX_STALE_S, MAX_SPREAD_BPS = 86_400, 10_000
DEFAULT_TIMEZONE = "Europe/Rome"
_CURRENCY = re.compile(r"^[A-Z0-9]{2,16}$")
PROFILE_FIELDS = (
    "id", "name", "max_order_notional_eur", "max_position_pct", "max_daily_loss_pct",
    "max_daily_loss_eur", "allowed_symbols", "allowed_asset_classes", "allowed_markets",
    "short_allowed", "description", "max_live_orders_per_hour",
)
_LIMIT_FIELDS = (
    "max_order_notional_eur", "max_position_pct", "max_daily_loss_pct", "max_daily_loss_eur",
)
_LIST_FIELDS = ("allowed_symbols", "allowed_asset_classes", "allowed_markets")
_PCT_FIELDS = ("max_position_pct", "max_daily_loss_pct")
FUSE_MIN, FUSE_MAX, FUSE_DEFAULT = 1, 1000, 30  # Ruling R8.1: real orders per hour


# --- risk profiles -------------------------------------------------------------------


def _profile_dto(profile: RiskProfile, portfolios_count: int | None = None) -> dict[str, Any]:
    out = {f: to_jsonable(getattr(profile, f)) for f in PROFILE_FIELDS}
    if portfolios_count is not None:
        out["portfolios_count"] = portfolios_count
    return out


def _duplicate_name(name: str) -> SMTError:
    return SMTError(
        ErrorCode.INVALID_ORDER, "esiste già un profilo con questo nome",
        {"field": "name", "name": name},
    )


def _string_list(values: list[str], field: str) -> list[str]:
    out: list[str] = []
    for v in values:
        cleaned = v.strip()
        if not cleaned or len(cleaned) > 64:
            raise _invalid(field, f"{field}: valore non valido")
        if cleaned not in out:
            out.append(cleaned)
    return out


async def _validated_profile(
    session: AsyncSession, raw: dict[str, Any], exclude: UUID | None
) -> dict[str, Any]:
    name = (raw["name"] or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise _invalid("name", f"il nome deve avere da 1 a {MAX_NAME_LEN} caratteri")
    out: dict[str, Any] = {"name": name, "short_allowed": bool(raw["short_allowed"])}
    description = (raw.get("description") or "").strip()
    if len(description) > MAX_DESCRIPTION_LEN:
        raise _invalid(
            "description", f"la descrizione non può superare {MAX_DESCRIPTION_LEN} caratteri"
        )
    out["description"] = description or None
    for f in _LIMIT_FIELDS:
        value = parse_decimal(raw[f], f)
        if value is not None and value <= 0:
            raise _invalid(f, f"{f} deve essere positivo")
        if value is not None and f in _PCT_FIELDS and value > 100:
            raise _invalid(f, f"{f} non può superare 100")
        out[f] = value
    for f in _LIST_FIELDS:
        out[f] = _string_list(raw[f], f)
    fuse = raw.get("max_live_orders_per_hour")
    fuse = FUSE_DEFAULT if fuse is None else fuse
    if isinstance(fuse, bool) or not isinstance(fuse, int) or not FUSE_MIN <= fuse <= FUSE_MAX:
        raise _invalid("max_live_orders_per_hour",
                       f"max_live_orders_per_hour deve essere un intero tra {FUSE_MIN} e"
                       f" {FUSE_MAX}")
    out["max_live_orders_per_hour"] = fuse
    for cls in out["allowed_asset_classes"]:
        parse_enum(AssetClass, cls, "allowed_asset_classes")
    taken = select(RiskProfile.id).where(RiskProfile.name == name)
    if exclude is not None:
        taken = taken.where(RiskProfile.id != exclude)
    if await session.scalar(taken.limit(1)) is not None:
        raise _duplicate_name(name)
    return out


@router.get("/risk-profiles")
async def list_risk_profiles(session: DbSession) -> list[dict]:
    """With `portfolios_count`: the live (non-archived) portfolios using each profile; a
    portfolio without a profile counts for Default (its effective profile)."""
    rows = list(await session.scalars(select(RiskProfile).order_by(RiskProfile.name)))
    counts = dict((await session.execute(
        select(Portfolio.risk_profile_id, func.count())
        .where(Portfolio.status != PortfolioStatus.archived)
        .group_by(Portfolio.risk_profile_id)
    )).all())
    out = []
    for p in rows:
        n = counts.get(p.id, 0)
        if p.name == DEFAULT_RISK_PROFILE_NAME:
            n += counts.get(None, 0)
        out.append(_profile_dto(p, n))
    return out


@router.post("/risk-profiles")
async def create_risk_profile(
    body: RiskProfileCreateIn, session: DbSession, redis: RedisClient
) -> dict:
    raw = body.model_dump()
    try:
        async with _transaction(session, redis):
            profile = RiskProfile(**await _validated_profile(session, raw, None))
            session.add(profile)
            await session.flush()
    except IntegrityError:  # a concurrent creation with the same name won the race
        raise _duplicate_name(raw["name"].strip()) from None
    return {**_profile_dto(profile), "name_warning": name_warning(profile.name)}


@router.patch("/risk-profiles/{profile_id}")
async def patch_risk_profile(
    profile_id: UUID, body: RiskProfilePatchIn, session: DbSession, redis: RedisClient
) -> dict:
    """Only the fields sent change (a limit sent as null removes it)."""
    profile = await session.get(RiskProfile, profile_id)
    if profile is None:
        raise SMTError(
            ErrorCode.NOT_FOUND, "profilo di rischio non trovato", {"profile_id": str(profile_id)}
        )
    raw = {f: getattr(profile, f) for f in PROFILE_FIELDS if f != "id"}
    for f in _LIMIT_FIELDS:
        raw[f] = None if raw[f] is None else decimal_to_str(raw[f])
    for f in body.model_fields_set:
        raw[f] = getattr(body, f)
    for f in ("name", "short_allowed", "max_live_orders_per_hour", *_LIST_FIELDS):
        if raw[f] is None:
            raise _invalid(f, f"{f} non può essere nullo")
    try:
        async with _transaction(session, redis):
            for key, value in (await _validated_profile(session, raw, profile_id)).items():
                setattr(profile, key, value)
            await session.flush()
    except IntegrityError:
        raise _duplicate_name(str(raw["name"]).strip()) from None
    return {**_profile_dto(profile), "name_warning": name_warning(profile.name)}


# --- interest rates ------------------------------------------------------------------


def _rate_dto(rate: InterestRate) -> dict[str, Any]:
    return {
        "kind": rate.kind.value, "key": rate.key, "annual_pct": to_jsonable(rate.annual_pct),
    }


async def _rates(session: AsyncSession) -> list[dict]:
    rows = await session.scalars(select(InterestRate).order_by(InterestRate.kind, InterestRate.key))
    return [_rate_dto(r) for r in rows]


@router.get("/interest-rates")
async def get_interest_rates(session: DbSession) -> list[dict]:
    return await _rates(session)


@router.put("/interest-rates")
async def put_interest_rates(
    body: list[InterestRateIn], session: DbSession, redis: RedisClient
) -> list[dict]:
    """Replace the whole table. Loan keys are currencies (or "*", the required fallback),
    borrow keys are asset classes."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[InterestKind, str]] = set()
    for item in body:
        kind = parse_enum(InterestKind, item.kind, "kind")
        key = item.key.strip()
        if kind == InterestKind.borrow:
            parse_enum(AssetClass, key, "key")
        elif key != "*" and not _CURRENCY.match(key):
            raise _invalid("key", "valuta non valida")
        if (kind, key) in seen:
            raise _invalid("key", f"tasso duplicato: {kind.value} {key}")
        seen.add((kind, key))
        pct = parse_decimal(item.annual_pct, "annual_pct")
        if pct is None or not 0 <= pct <= MAX_RATE_PCT:
            raise _invalid("annual_pct", f"annual_pct deve essere tra 0 e {MAX_RATE_PCT}")
        rows.append({"kind": kind, "key": key, "annual_pct": pct})
    if (InterestKind.loan, "*") not in seen:
        raise _invalid("key", 'manca il tasso di ripiego dei prestiti ("*")')
    async with _transaction(session, redis):
        await session.execute(delete(InterestRate))
        await session.execute(insert(InterestRate).values(rows))
    return await _rates(session)


# --- app settings --------------------------------------------------------------------


def _hide_mode(value: Any) -> bool:
    return bool(value.get("on", True)) if isinstance(value, dict) else True


async def _app_settings(session: AsyncSession) -> dict[str, Any]:
    stored = dict((await session.execute(select(AppSetting.key, AppSetting.value))).all())
    stale = {**DEFAULT_STALE_QUOTE_S, **(stored.get("stale_quote_s") or {})}
    spread = {**DEFAULT_SPREAD_BPS, **(stored.get("default_spread_bps") or {})}
    return {
        "stale_quote_s": stale,
        "default_spread_bps": spread,
        "timezone": stored.get("timezone") or DEFAULT_TIMEZONE,
        "mcp_hide_mode": _hide_mode(stored.get("mcp_hide_mode")),
    }


def _per_class(values: dict[str, int], field: str, lo: int, hi: int) -> dict[str, int]:
    for cls, v in values.items():
        parse_enum(AssetClass, cls, field)
        if isinstance(v, bool) or not lo <= v <= hi:
            raise _invalid(field, f"{field}.{cls} deve essere un intero tra {lo} e {hi}")
    return values


async def _store_setting(session: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(AppSetting).values(key=key, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))


@router.get("/settings/app")
async def get_app_settings(session: DbSession) -> dict:
    return await _app_settings(session)


@router.put("/settings/app")
async def put_app_settings(body: AppSettingsIn, session: DbSession, redis: RedisClient) -> dict:
    updates: dict[str, Any] = {}
    current = await _app_settings(session)
    if body.stale_quote_s is not None:
        new = _per_class(body.stale_quote_s, "stale_quote_s", 1, MAX_STALE_S)
        updates["stale_quote_s"] = {**current["stale_quote_s"], **new}
    if body.default_spread_bps is not None:
        new = _per_class(body.default_spread_bps, "default_spread_bps", 0, MAX_SPREAD_BPS)
        updates["default_spread_bps"] = {**current["default_spread_bps"], **new}
    if body.timezone is not None:
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise _invalid("timezone", "fuso orario non valido") from None
        updates["timezone"] = body.timezone
    if body.mcp_hide_mode is not None:
        updates["mcp_hide_mode"] = {"on": body.mcp_hide_mode}
    async with _transaction(session, redis):
        for key, value in updates.items():
            await _store_setting(session, key, value)
    return await _app_settings(session)


# --- kill switch ---------------------------------------------------------------------


@router.get("/kill-switch")
async def get_kill_switch(session: DbSession) -> dict:
    return {"on": await is_kill_switch_on(session)}


@router.post("/kill-switch")
async def post_kill_switch(body: KillSwitchIn, session: DbSession, redis: RedisClient) -> dict:
    """The `kill_switch` event goes out after the commit (post-commit dispatch)."""
    async with _transaction(session, redis):
        canceled = await set_kill_switch_and_cancel(
            session, body.on, cancel_open=body.cancel_open, by="admin"
        )
    return {"on": body.on, "canceled": canceled}
