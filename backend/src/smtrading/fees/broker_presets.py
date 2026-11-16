"""The fee preset of each broker: what the portal proposes for a portfolio's fee rule when
the portfolio is linked to that broker (user decision 2026-11-16 22:15).

One table, edited here: commission (pct + fixed) and the FX fee per broker family, with
`source_note` shown in the UI (public price lists, to be verified). Applying a preset
(`apply_broker_preset`) creates or updates the portfolio-scope rule named
«<portafoglio> – <broker>» and deactivates the portfolio's other active portfolio-scope
rules (e.g. «<portafoglio> – zero commissioni»): rules are never deleted, journals
reference them. The request itself is the audit row (activity log `fees.preset_apply`).

Applying a preset never touches the broker link, arming or the live gate: it is a
separate, confirmed step after (or alongside) the link.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.money import decimal_to_str
from smtrading.fees.models import FeeRule, FeeScope, FeeSide
from smtrading.portfolios.locks import lock_live_portfolio

SOURCE_NOTE = "listino pubblico, da verificare"
NAME_SEP = " – "
MAX_NAME_LEN = 128


@dataclass(frozen=True)
class BrokerFeePreset:
    broker: str  # the label in the rule name and the UI
    pct: Decimal  # commission, % of the notional
    fixed: Decimal  # commission, fixed per order
    fx_pct: Decimal  # FX conversion fee, % of the converted amount
    note: str  # what the preset covers, shown in the UI
    source_note: str = SOURCE_NOTE


PRESETS: dict[str, BrokerFeePreset] = {
    "alpaca": BrokerFeePreset(
        broker="Alpaca", pct=Decimal(0), fixed=Decimal(0), fx_pct=Decimal(0),
        note="commissione 0 su azioni ed ETF USA; conto in USD, nessun costo di cambio",
    ),
    "t212": BrokerFeePreset(
        broker="Trading 212", pct=Decimal(0), fixed=Decimal(0), fx_pct=Decimal("0.15"),
        note="commissione 0; cambio valuta 0,15%",
    ),
    "etoro": BrokerFeePreset(
        broker="eToro", pct=Decimal(0), fixed=Decimal(0), fx_pct=Decimal("0.5"),
        note="commissione 0 su azioni ed ETF (la commissione reale arriva dalle esecuzioni); "
             "cambio valuta 0,5%",
    ),
}

# every broker link kind, in display order, with its preset family
BROKER_KINDS: dict[str, str] = {
    "alpaca_paper": "alpaca",
    "alpaca_live": "alpaca",
    "t212_demo": "t212",
    "t212_live": "t212",
    "etoro_demo": "etoro",
    "etoro_live": "etoro",
}


def preset_for(kind: str) -> BrokerFeePreset | None:
    family = BROKER_KINDS.get(kind)
    return PRESETS[family] if family else None


def preset_rule_name(portfolio_name: str, preset: BrokerFeePreset) -> str:
    """«<portafoglio> – <broker>», the portfolio name shortened to fit the column."""
    suffix = NAME_SEP + preset.broker
    return portfolio_name.strip()[: MAX_NAME_LEN - len(suffix)].rstrip() + suffix


def preset_dto(kind: str, preset: BrokerFeePreset) -> dict[str, Any]:
    return {
        "broker_kind": kind, "broker": preset.broker, "pct": decimal_to_str(preset.pct),
        "fixed": decimal_to_str(preset.fixed), "fx_pct": decimal_to_str(preset.fx_pct),
        "note": preset.note, "source_note": preset.source_note,
        "rule_name_suffix": preset.broker,
    }


def presets_dto() -> list[dict[str, Any]]:
    return [preset_dto(kind, PRESETS[family]) for kind, family in BROKER_KINDS.items()]


@dataclass
class AppliedPreset:
    rule: FeeRule
    created: bool
    deactivated: list[FeeRule]


async def apply_broker_preset(
    session: AsyncSession, portfolio_id: uuid.UUID, preset: BrokerFeePreset
) -> AppliedPreset:
    """Creates or updates (and reactivates) the portfolio-scope rule «<portafoglio> –
    <broker>» with the preset's values (both sides, the portfolio's base currency, no
    min/max/tiers) and deactivates the portfolio's other active portfolio-scope rules.
    The portfolio row is locked (serialises concurrent applies); archived: refused."""
    portfolio = await lock_live_portfolio(session, portfolio_id)
    ref = str(portfolio.id)
    name = preset_rule_name(portfolio.name, preset)
    rules = (await session.scalars(
        select(FeeRule).where(FeeRule.scope == FeeScope.portfolio, FeeRule.scope_ref == ref)
        .order_by(FeeRule.created_at, FeeRule.id)
    )).all()
    rule = next((r for r in rules if r.name == name), None)
    created = rule is None
    if rule is None:
        rule = FeeRule(name=name, scope=FeeScope.portfolio, scope_ref=ref)
        session.add(rule)
    rule.side = FeeSide.both
    rule.currency = portfolio.base_currency
    rule.fixed, rule.pct, rule.fx_pct = preset.fixed, preset.pct, preset.fx_pct
    rule.min_fee = rule.max_fee = rule.tiers = None
    rule.priority = 0
    rule.active = True
    deactivated = [r for r in rules if r is not rule and r.active]
    for other in deactivated:
        other.active = False
    await session.flush()
    await session.refresh(rule)
    return AppliedPreset(rule=rule, created=created, deactivated=deactivated)
