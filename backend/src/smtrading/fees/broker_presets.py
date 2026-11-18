"""The fee preset of each broker: what the portal proposes for a portfolio's fee rule when
the portfolio is linked to that broker (user decision 2026-11-16 22:15).

One table, edited here: per broker family a set of entries (commission pct + fixed and FX
fee; `asset_class` None for stocks/ETF and every class without its own entry), each with
the `source_note` shown in the UI. Applying a preset (`apply_broker_preset`) creates or
updates one portfolio-scope rule per entry, «<portafoglio> – <broker>» or
«<portafoglio> – <broker> · <classe>», and deactivates the portfolio's other active portfolio-scope
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

NAME_SEP = " – "
CLASS_SEP = " · "
MAX_NAME_LEN = 128
CLASS_LABELS = {"crypto": "Crypto", "us_equity": "Azioni USA", "eu_equity": "Azioni UE",
                "forex": "Forex"}


@dataclass(frozen=True)
class PresetEntry:
    """One rule of a preset: `asset_class` None covers every class (stocks and ETFs here)
    that no class entry of the same preset covers."""

    asset_class: str | None
    pct: Decimal  # commission, % of the notional
    fixed: Decimal  # commission, fixed per order
    fx_pct: Decimal  # FX conversion fee, % of the converted amount
    note: str  # what the entry covers, shown in the UI
    source_note: str  # how sure the figures are, shown in the UI


@dataclass(frozen=True)
class BrokerFeePreset:
    broker: str  # the label in the rule names and the UI
    entries: tuple[PresetEntry, ...]


_Z = Decimal(0)

# Sources, checked 2026-11-18 (official pages only; the UI shows `source_note`):
# - Alpaca stocks/ETF: https://alpaca.markets/support/commission-clearing-fees («no
#   commission charges for Alpaca self-directed individual cash brokerage accounts»
#   trading US listed securities through an API; SEC/FINRA fees on sells may apply).
# - Alpaca crypto: https://docs.alpaca.markets/docs/crypto-fees (tier 1, 30-day volume
#   0-100k USD: maker 0.15%, taker 0.25%; the preset takes the taker fee).
# - Trading 212: https://helpcentre.trading212.com/hc/en-us/articles/11669719976093
#   (FX fee 0.15%; none when trading in the asset's currency) and zero commission.
# - eToro: https://www.etoro.com/trading/fees/ (ETF zero commission; stocks «a
#   commission fee of $1 or $2 may apply» by country and exchange, no figures in the
#   table: left at 0, the real fee comes from the fills; crypto flat 1% for
#   Bronze/Silver/Gold) and https://www.etoro.com/trading/fees/conversion/ («Local
#   currency account <> USD 0.75%» on trades, Club discounts apart).
_CHECKED = "verificato 18/11/2026"
_ALPACA_STOCKS = (f"listino ufficiale Alpaca, {_CHECKED}: "
                  "alpaca.markets/support/commission-clearing-fees")
_ALPACA_CRYPTO = (f"listino ufficiale Alpaca (livello base, taker), {_CHECKED}: "
                  "docs.alpaca.markets/docs/crypto-fees")
_T212_SOURCE = (f"centro assistenza Trading 212, {_CHECKED}: helpcentre.trading212.com "
                "(multi-currency account)")
_ETORO_STOCKS = (f"listino ufficiale eToro, {_CHECKED}: etoro.com/trading/fees; cambio "
                 "0,75% confermato (etoro.com/trading/fees/conversion); commissione azioni "
                 "1-2 $ secondo paese e borsa, da verificare")
_ETORO_CRYPTO = (f"listino ufficiale eToro (livelli Bronze-Gold), {_CHECKED}: "
                 "etoro.com/trading/fees e /conversion")

PRESETS: dict[str, BrokerFeePreset] = {
    "alpaca": BrokerFeePreset("Alpaca", (
        PresetEntry(None, _Z, _Z, _Z, "azioni ed ETF: commissione 0 (sulle vendite "
                    "possibili piccole tasse SEC/FINRA); conto in USD, nessun costo di "
                    "cambio", _ALPACA_STOCKS),
        PresetEntry("crypto", Decimal("0.25"), _Z, _Z, "crypto: commissione 0,25%",
                    _ALPACA_CRYPTO),
    )),
    "t212": BrokerFeePreset("Trading 212", (
        PresetEntry(None, _Z, _Z, Decimal("0.15"), "commissione 0; cambio valuta 0,15%",
                    _T212_SOURCE),
    )),
    "etoro": BrokerFeePreset("eToro", (
        PresetEntry(None, _Z, _Z, Decimal("0.75"), "azioni ed ETF: commissione 0 (la "
                    "commissione reale arriva dalle esecuzioni); cambio valuta 0,75%",
                    _ETORO_STOCKS),
        PresetEntry("crypto", Decimal(1), _Z, Decimal("0.75"),
                    "crypto: commissione 1%; cambio valuta 0,75%", _ETORO_CRYPTO),
    )),
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


def preset_rule_name(portfolio_name: str, preset: BrokerFeePreset,
                     asset_class: str | None = None) -> str:
    """«<portafoglio> – <broker>» or «<portafoglio> – <broker> · <classe>», the portfolio
    name shortened to fit the column."""
    suffix = NAME_SEP + preset.broker
    if asset_class is not None:
        suffix += CLASS_SEP + CLASS_LABELS.get(asset_class, asset_class)
    return portfolio_name.strip()[: MAX_NAME_LEN - len(suffix)].rstrip() + suffix


def _entry_dto(e: PresetEntry) -> dict[str, Any]:
    return {
        "asset_class": e.asset_class, "pct": decimal_to_str(e.pct),
        "fixed": decimal_to_str(e.fixed), "fx_pct": decimal_to_str(e.fx_pct),
        "note": e.note, "source_note": e.source_note,
    }


def preset_dto(kind: str, preset: BrokerFeePreset) -> dict[str, Any]:
    return {"broker_kind": kind, "broker": preset.broker,
            "entries": [_entry_dto(e) for e in preset.entries]}


def presets_dto() -> list[dict[str, Any]]:
    return [preset_dto(kind, PRESETS[family]) for kind, family in BROKER_KINDS.items()]


@dataclass
class AppliedPreset:
    rules: list[FeeRule]  # one per preset entry, in the preset's order
    created: list[FeeRule]
    deactivated: list[FeeRule]


async def apply_broker_preset(
    session: AsyncSession, portfolio_id: uuid.UUID, preset: BrokerFeePreset
) -> AppliedPreset:
    """For each preset entry, creates or updates (and reactivates) the portfolio-scope rule
    «<portafoglio> – <broker>[ · <classe>]» with the entry's values (its asset class, both
    sides, the portfolio's base currency, no min/max/tiers); then deactivates the
    portfolio's other active portfolio-scope rules. The portfolio row is locked
    (serialises concurrent applies); archived: refused."""
    portfolio = await lock_live_portfolio(session, portfolio_id)
    ref = str(portfolio.id)
    existing = (await session.scalars(
        select(FeeRule).where(FeeRule.scope == FeeScope.portfolio, FeeRule.scope_ref == ref)
        .order_by(FeeRule.created_at, FeeRule.id)
    )).all()
    by_name = {r.name: r for r in existing}
    rules: list[FeeRule] = []
    created: list[FeeRule] = []
    for entry in preset.entries:
        name = preset_rule_name(portfolio.name, preset, entry.asset_class)
        rule = by_name.get(name)
        if rule is None:
            rule = FeeRule(name=name, scope=FeeScope.portfolio, scope_ref=ref)
            session.add(rule)
            created.append(rule)
        rule.asset_class = entry.asset_class
        rule.side = FeeSide.both
        rule.currency = portfolio.base_currency
        rule.fixed, rule.pct, rule.fx_pct = entry.fixed, entry.pct, entry.fx_pct
        rule.min_fee = rule.max_fee = rule.tiers = None
        rule.priority = 0
        rule.active = True
        rules.append(rule)
    deactivated = [r for r in existing if r.active and all(r is not x for x in rules)]
    for other in deactivated:
        other.active = False
    await session.flush()
    for rule in rules:
        await session.refresh(rule)
    return AppliedPreset(rules=rules, created=created, deactivated=deactivated)
