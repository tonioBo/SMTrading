from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.fees.calculator import FeeRuleData, Tier, compute_trade_fee
from smtrading.fees.models import FeeRule, FeeScope
from smtrading.instruments.models import AssetClass

_SPECIFICITY = {FeeScope.portfolio: 2, FeeScope.asset_class: 1, FeeScope.global_: 0}


def _specificity(rule: FeeRuleData) -> int:
    """portfolio + asset class (3) > portfolio (2) > asset class (1) > global (0)."""
    if rule.scope == FeeScope.portfolio and rule.asset_class is not None:
        return 3
    return _SPECIFICITY[rule.scope]


def _matches(
    rule: FeeRuleData, portfolio_id: UUID, asset_class: AssetClass, side: str
) -> bool:
    if not rule.active or rule.side not in (side, "both"):
        return False
    if rule.scope == FeeScope.portfolio:
        return rule.scope_ref == str(portfolio_id) and rule.asset_class in (
            None, asset_class.value)
    if rule.scope == FeeScope.asset_class:
        return rule.scope_ref == asset_class.value
    return True


def resolve_rule(
    rules: Sequence[FeeRuleData],
    *,
    portfolio_id: UUID,
    asset_class: AssetClass,
    side: str,
) -> FeeRuleData:
    """Most specific active rule wins (portfolio + asset class > portfolio > asset class >
    global); ties by priority, then newest created_at."""
    side = str(side)
    candidates = [r for r in rules if _matches(r, portfolio_id, asset_class, side)]
    if not candidates:
        raise SMTError(
            ErrorCode.FEE_RULE_NOT_FOUND,
            "No applicable fee rule",
            {
                "portfolio_id": str(portfolio_id),
                "asset_class": asset_class.value,
                "side": side,
            },
        )
    return max(candidates, key=lambda r: (_specificity(r), r.priority, r.created_at))


def _to_data(row: FeeRule) -> FeeRuleData:
    tiers = None
    if row.tiers:
        tiers = tuple(
            Tier(
                up_to=None if t["up_to"] is None else Decimal(str(t["up_to"])),
                pct=Decimal(str(t["pct"])),
            )
            for t in row.tiers
        )
    return FeeRuleData(
        id=row.id,
        scope=row.scope,
        scope_ref=row.scope_ref,
        side=row.side,
        currency=row.currency,
        fixed=row.fixed,
        pct=row.pct,
        min_fee=row.min_fee,
        max_fee=row.max_fee,
        tiers=tiers,
        fx_pct=row.fx_pct,
        priority=row.priority,
        active=row.active,
        created_at=row.created_at,
        asset_class=row.asset_class,
    )


async def load_rules(session: AsyncSession) -> list[FeeRuleData]:
    rows = (await session.execute(select(FeeRule))).scalars().all()
    return [_to_data(r) for r in rows]


@dataclass
class FeeQuote:
    rule_id: UUID
    amount: Decimal
    currency: str


async def quote_fee(
    session: AsyncSession,
    *,
    portfolio_id: UUID,
    asset_class: AssetClass,
    side: str,
    notional: Decimal,
) -> FeeQuote:
    """`notional` must already be expressed in the matching rule's currency."""
    rule = resolve_rule(
        await load_rules(session),
        portfolio_id=portfolio_id,
        asset_class=asset_class,
        side=side,
    )
    return FeeQuote(rule.id, compute_trade_fee(rule, notional), rule.currency)
