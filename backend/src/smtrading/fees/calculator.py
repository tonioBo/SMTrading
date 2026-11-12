import decimal
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import quantize_money
from smtrading.fees.models import FeeScope, FeeSide

_PREC = 60


@dataclass(frozen=True)
class Tier:
    up_to: Decimal | None
    pct: Decimal


@dataclass(frozen=True)
class FeeRuleData:
    id: UUID
    scope: FeeScope
    scope_ref: str | None
    side: FeeSide
    currency: str
    fixed: Decimal
    pct: Decimal
    min_fee: Decimal | None
    max_fee: Decimal | None
    tiers: tuple[Tier, ...] | None
    fx_pct: Decimal
    priority: int
    active: bool
    created_at: datetime


def _tier_pct(tiers: tuple[Tier, ...], notional: Decimal) -> Decimal:
    for tier in tiers:
        if tier.up_to is None or notional <= tier.up_to:
            return tier.pct
    return tiers[-1].pct


def compute_trade_fee(rule: FeeRuleData, notional: Decimal) -> Decimal:
    """Fee for `notional` (in the rule's currency); tier pct applies to the whole notional."""
    if notional < 0:
        raise SMTError(ErrorCode.INVALID_AMOUNT, "Notional must not be negative")
    with decimal.localcontext() as ctx:
        ctx.prec = _PREC
        pct = _tier_pct(rule.tiers, notional) if rule.tiers else rule.pct
        fee = rule.fixed + notional * pct / 100
        if rule.min_fee is not None:
            fee = max(fee, rule.min_fee)
        if rule.max_fee is not None:
            fee = min(fee, rule.max_fee)
        return quantize_money(fee, rule.currency)


def compute_fx_fee(rule: FeeRuleData, converted_amount: Decimal, currency: str) -> Decimal:
    with decimal.localcontext() as ctx:
        ctx.prec = _PREC
        return quantize_money(abs(converted_amount) * rule.fx_pct / 100, currency)
