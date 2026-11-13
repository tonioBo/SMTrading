"""Admin API: fee rules (never deleted: DELETE deactivates) and the fee simulator."""

import re
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import DbSession, RedisClient, require_admin
from smtrading.admin.common import Quotes, _invalid, _transaction
from smtrading.admin.schemas import FeeRuleCreateIn, FeeRulePatchIn, FeeSimulateIn
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.engine.models import OrderSide, OrderType
from smtrading.engine.service import PlaceOrderRequest, preview_order
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.instruments.models import AssetClass
from smtrading.mcp.dto import parse_decimal, parse_enum, to_jsonable
from smtrading.portfolios.models import Portfolio

router = APIRouter(prefix="/api", tags=["fees"], dependencies=[Depends(require_admin)])

MAX_NAME_LEN = 128
MAX_PRIORITY = 1_000_000
_CURRENCY = re.compile(r"^[A-Z0-9]{2,16}$")
RULE_FIELDS = (
    "id", "name", "scope", "scope_ref", "side", "currency", "fixed", "pct", "min_fee",
    "max_fee", "tiers", "fx_pct", "priority", "active", "created_at",
)
_VALUE_FIELDS = ("fixed", "pct", "min_fee", "max_fee", "fx_pct")


def _rule_dto(rule: FeeRule) -> dict[str, Any]:
    return {f: to_jsonable(getattr(rule, f)) for f in RULE_FIELDS}


async def _rule(session: AsyncSession, rule_id: UUID) -> FeeRule:
    rule = await session.get(FeeRule, rule_id)
    if rule is None:
        raise SMTError(ErrorCode.NOT_FOUND, "regola non trovata", {"rule_id": str(rule_id)})
    return rule


def _non_negative(value: Any, field: str, *, default: Decimal | None = None) -> Decimal | None:
    parsed = parse_decimal(value, field)
    if parsed is None:
        return default
    if parsed < 0:
        raise _invalid(field, f"{field} non può essere negativo")
    return parsed


def _tiers(raw: list[dict[str, Any]] | None) -> list[dict[str, str | None]] | None:
    """Ascending by up_to, the last with up_to=null, pct >= 0; stored as strings (JSONB)."""
    if not raw:
        return None
    out: list[dict[str, str | None]] = []
    previous: Decimal | None = None
    for i, tier in enumerate(raw):
        last = i == len(raw) - 1
        up_to = parse_decimal(tier.get("up_to"), "tiers")
        pct = _non_negative(tier.get("pct"), "tiers")
        if pct is None:
            raise _invalid("tiers", "ogni scaglione richiede pct")
        if last:
            if up_to is not None:
                raise _invalid("tiers", "l'ultimo scaglione deve avere up_to nullo")
        else:
            if up_to is None or up_to <= 0:
                raise _invalid("tiers", "up_to deve essere positivo (solo l'ultimo è nullo)")
            if previous is not None and up_to <= previous:
                raise _invalid("tiers", "gli scaglioni devono essere in ordine crescente di up_to")
            previous = up_to
        out.append(
            {"up_to": None if up_to is None else decimal_to_str(up_to), "pct": decimal_to_str(pct)}
        )
    return out


async def _validated(session: AsyncSession, raw: dict[str, Any], exclude: UUID | None) -> dict:
    """The column values of a complete rule `raw`, validated; raises 400/404."""
    name = (raw["name"] or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        raise _invalid("name", f"il nome deve avere da 1 a {MAX_NAME_LEN} caratteri")
    scope = parse_enum(FeeScope, raw["scope"], "scope")
    side = parse_enum(FeeSide, raw["side"], "side")
    if scope is None or side is None:
        raise _invalid("scope", "scope e side obbligatori")
    scope_ref = raw["scope_ref"]
    if scope == FeeScope.global_:
        if scope_ref is not None:
            raise _invalid("scope_ref", "scope_ref deve essere nullo per una regola globale")
    elif not scope_ref:
        raise _invalid("scope_ref", "scope_ref obbligatorio per questo ambito")
    elif scope == FeeScope.asset_class:
        parse_enum(AssetClass, scope_ref, "scope_ref")
    else:
        try:
            exists = await session.get(Portfolio, UUID(scope_ref)) is not None
        except ValueError:
            exists = False
        if not exists:
            raise _invalid("scope_ref", "portafoglio non trovato")
    currency = (raw["currency"] or "").strip().upper()
    if not _CURRENCY.match(currency):
        raise _invalid("currency", "valuta non valida")
    values = {f: _non_negative(raw[f], f) for f in _VALUE_FIELDS}
    for f in ("fixed", "pct", "fx_pct"):
        if values[f] is None:
            values[f] = Decimal(0)
    lo, hi = values["min_fee"], values["max_fee"]
    if lo is not None and hi is not None and lo > hi:
        raise _invalid("min_fee", "min_fee non può superare max_fee")
    priority = raw["priority"]
    if abs(priority) > MAX_PRIORITY:
        raise _invalid("priority", "priorità fuori intervallo")
    taken = select(FeeRule.id).where(
        FeeRule.name == name, FeeRule.scope_ref.is_not_distinct_from(scope_ref)
    )
    if exclude is not None:
        taken = taken.where(FeeRule.id != exclude)
    if await session.scalar(taken.limit(1)) is not None:
        raise _duplicate_name(name)
    return {
        "name": name, "scope": scope, "scope_ref": scope_ref, "side": side,
        "currency": currency, **values, "tiers": _tiers(raw["tiers"]), "priority": priority,
        "active": bool(raw["active"]),
    }


def _duplicate_name(name: str) -> SMTError:
    return SMTError(
        ErrorCode.INVALID_ORDER, "esiste già una regola con questo nome per lo stesso ambito",
        {"field": "name", "name": name},
    )


async def _require_global_rule(session: AsyncSession) -> None:
    """Every order needs a rule: an active global rule for both sides must always remain."""
    await session.flush()
    count = await session.scalar(
        select(func.count()).select_from(FeeRule).where(
            FeeRule.scope == FeeScope.global_, FeeRule.side == FeeSide.both,
            FeeRule.active.is_(True),
        )
    )
    if not count:
        raise SMTError(
            ErrorCode.INVALID_ORDER,
            "Deve restare una regola globale attiva per entrambi i lati",
            {"field": "active"},
        )


def _tier_dicts(tiers) -> list[dict[str, Any]] | None:
    return None if tiers is None else [t.model_dump() for t in tiers]


@router.get("/fee-rules")
async def list_fee_rules(session: DbSession) -> list[dict]:
    rows = await session.scalars(select(FeeRule).order_by(FeeRule.created_at, FeeRule.id))
    return [_rule_dto(r) for r in rows]


@router.post("/fee-rules")
async def create_fee_rule(body: FeeRuleCreateIn, session: DbSession, redis: RedisClient) -> dict:
    raw = body.model_dump()
    raw["tiers"] = _tier_dicts(body.tiers)
    try:
        async with _transaction(session, redis):
            rule = FeeRule(**await _validated(session, raw, None))
            session.add(rule)
            await session.flush()
            await session.refresh(rule)
    except IntegrityError:
        raise _duplicate_name(str(raw["name"]).strip()) from None
    return _rule_dto(rule)


@router.patch("/fee-rules/{rule_id}")
async def patch_fee_rule(
    rule_id: UUID, body: FeeRulePatchIn, session: DbSession, redis: RedisClient
) -> dict:
    """Only the fields sent change; the resulting rule is validated as a whole."""
    rule = await _rule(session, rule_id)
    raw = {f: getattr(rule, f) for f in RULE_FIELDS if f not in ("id", "created_at")}
    raw["scope"], raw["side"] = rule.scope.value, rule.side.value
    for f in _VALUE_FIELDS:
        raw[f] = None if raw[f] is None else decimal_to_str(raw[f])
    for f in body.model_fields_set:
        raw[f] = getattr(body, f)
    raw["tiers"] = _tier_dicts(body.tiers) if "tiers" in body.model_fields_set else rule.tiers
    for f in ("name", "scope", "side", "currency", "fixed", "pct", "fx_pct", "priority", "active"):
        if raw[f] is None:
            raise _invalid(f, f"{f} non può essere nullo")
    try:
        async with _transaction(session, redis):
            for key, value in (await _validated(session, raw, rule_id)).items():
                setattr(rule, key, value)
            await _require_global_rule(session)
    except IntegrityError:
        raise _duplicate_name(str(raw["name"]).strip()) from None
    return _rule_dto(rule)


@router.delete("/fee-rules/{rule_id}")
async def deactivate_fee_rule(rule_id: UUID, session: DbSession, redis: RedisClient) -> dict:
    """Rules are never deleted (journals reference `fee_rule_id`): this sets active=false."""
    rule = await _rule(session, rule_id)
    async with _transaction(session, redis):
        rule.active = False
        await _require_global_rule(session)
    return _rule_dto(rule)


@router.post("/fee-rules/simulate")
async def simulate_fee(body: FeeSimulateIn, session: DbSession, qs: Quotes) -> dict:
    """The fee of a hypothetical market order, from the engine's pre-trade check."""
    side = parse_enum(OrderSide, body.side, "side")
    qty = parse_decimal(body.qty, "qty")
    notional = parse_decimal(body.notional, "notional")
    if (qty is None) == (notional is None):
        raise _invalid("qty", "indicare esattamente uno tra qty e notional")
    request = PlaceOrderRequest(
        portfolio_id=body.portfolio_id, symbol=body.symbol, side=side, type=OrderType.market,
        qty=qty, notional=notional, source="admin",
    )
    result = await preview_order(session, qs, request)
    if not result.ok or result.estimated_fee is None:
        raise SMTError(result.code or ErrorCode.INVALID_ORDER, result.message or "simulazione non riuscita")
    quote = result.estimated_fee
    rule = await _rule(session, quote.rule_id)
    return {
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "notional_eur": to_jsonable(result.estimated_notional_eur),
        "fee": to_jsonable(quote.amount),
        "fee_currency": quote.currency,
        "fx_fee_eur": to_jsonable(result.estimated_fx_fee_eur),
    }
