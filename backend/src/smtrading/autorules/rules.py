"""Managing the automatic rules (mode B): create, edit (a new version each time),
activate, pause, resume, brake, pause every rule of a portfolio, and the read of the
active rules for the `scalper`.

Every function works in the caller's transaction and never commits. The price stream
follows a rule's symbol while it is active (source "auto_rules"): `queue_track` /
`queue_untrack`, run after the commit by the caller's `dispatch_post_commit`.

Activation (and resume) takes the portfolio row lock first, then the rule's (R11), and
checks in order: the portfolio is not archived and has `auto_rules_enabled`; no other
active rule on the same (portfolio, symbol) (the partial unique index is the guarantee,
the check gives the text); no position or open order of the portfolio on the symbol that
is not the rule's; with mode A on, the symbol is not in an approved intraday strategy of
the portfolio."""
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.autorules.models import Brakes, validate_brakes, validate_params
from smtrading.autorules.tables import (
    ACTIVE,
    BRAKED,
    DRAFT,
    OPEN,
    PAUSED,
    SENT,
    AutoRule,
    AutoRuleVersion,
    AutoTrade,
)
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OPEN_STATUSES, Order
from smtrading.events import json_safe, queue_track, queue_untrack
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.intraday.models import INTRADAY_TIMEFRAMES
from smtrading.portfolios.locks import lock_portfolio
from smtrading.portfolios.models import Portfolio, PortfolioStatus, ensure_not_archived
from smtrading.positions.models import Position

log = logging.getLogger(__name__)

STREAM_SOURCE = "auto_rules"
ALLOWED_CLASSES = (AssetClass.us_equity, AssetClass.crypto)

ONLY_US_CRYPTO = "solo azioni USA e cripto"
SIZE_MESSAGE = "indica la quantità o l'importo (uno solo), maggiore di 0"
PAUSE_FIRST = "metti in pausa la regola prima di modificarla"
SWITCH_OFF = "le regole automatiche sono spente su questo portafoglio"
ACTIVE_TAKEN = "c'è già una regola attiva su questo simbolo in questo portafoglio"
POSITION_OPEN = ("il portafoglio ha una posizione aperta su questo simbolo: chiudila prima "
                 "di attivare la regola")
ORDER_OPEN = ("il portafoglio ha un ordine aperto su questo simbolo: annullalo prima di "
              "attivare la regola")
MODE_A_OWNS = "il simbolo è già seguito dal lavoro in giornata con l'agente su questo portafoglio"
NOT_ACTIVATABLE = "la regola si attiva da bozza; da pausa o freni si riprende"
NOT_RESUMABLE = "si riprende solo una regola in pausa o fermata dai freni"
# the reason of the rules paused when the portfolio's switch is turned off
SWITCH_OFF_REASON = "interruttore spento"
NOT_PAUSABLE = "la regola è una bozza: non c'è niente da mettere in pausa"


def _invalid(message: str, rule: AutoRule | None = None, **details) -> SMTError:
    if rule is not None:
        details = {"rule_id": str(rule.id), **details}
    return SMTError(ErrorCode.INVALID_ORDER, message, details)


def _size(qty: Decimal | None, notional: Decimal | None) -> tuple[Decimal | None,
                                                                   Decimal | None]:
    if (qty is None) == (notional is None):
        raise _invalid(SIZE_MESSAGE, field="qty")
    value = qty if qty is not None else notional
    if not isinstance(value, Decimal) or not value.is_finite() or not value > 0:
        raise _invalid(SIZE_MESSAGE, field="qty" if qty is not None else "notional")
    return qty, notional


def _brakes_json(brakes: dict | None) -> dict:
    return json_safe(asdict(validate_brakes(brakes or {}, level="rule")))


async def _rule(session: AsyncSession, rule_id: uuid.UUID, *, lock: bool = True) -> AutoRule:
    stmt = select(AutoRule).where(AutoRule.id == rule_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rule = (await session.execute(stmt)).scalar_one_or_none()
    if rule is None:
        raise SMTError(ErrorCode.NOT_FOUND, "regola non trovata", {"rule_id": str(rule_id)})
    return rule


async def _current_version(session: AsyncSession, rule: AutoRule) -> AutoRuleVersion:
    return await session.get_one(AutoRuleVersion, (rule.id, rule.version))


def _set_status(rule: AutoRule, status: str, reason: str | None, now: datetime) -> None:
    rule.status = status
    rule.status_reason = reason[:200] if reason else None
    rule.status_changed_at = now


async def create_rule(
    session: AsyncSession, *, portfolio_id: uuid.UUID, symbol: str, model: str,
    params: dict, qty: Decimal | None = None, notional: Decimal | None = None,
    brakes: dict | None = None, by: str,
) -> AutoRule:
    """A draft rule, version 1, with validated parameters, size and brakes; the instrument
    must be an active US equity or crypto."""
    portfolio = await session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato",
                       {"portfolio_id": str(portfolio_id)})
    ensure_not_archived(portfolio)
    sym = canonical_symbol(symbol)
    instrument = await session.get(Instrument, sym)
    if instrument is None or not instrument.active \
            or instrument.asset_class not in ALLOWED_CLASSES:
        raise _invalid(ONLY_US_CRYPTO, symbol=sym)
    clean = json_safe(validate_params(model, params))
    qty, notional = _size(qty, notional)
    rule = AutoRule(id=uuid.uuid4(), portfolio_id=portfolio_id, symbol=sym, model=model,
                    status=DRAFT, version=1, status_changed_at=datetime.now(UTC))
    session.add(rule)
    await session.flush()
    session.add(AutoRuleVersion(rule_id=rule.id, version=1, params=clean, qty=qty,
                                notional=notional, brakes=_brakes_json(brakes),
                                created_by=by[:64]))
    await session.flush()
    return rule


async def update_rule(
    session: AsyncSession, rule_id: uuid.UUID, *, params: dict | None = None,
    qty: Decimal | None = None, notional: Decimal | None = None,
    brakes: dict | None = None, by: str,
) -> AutoRule:
    """A new version (version + 1) with what is sent replacing the current values
    (`params` and `brakes` whole; `qty` or `notional` replaces the size). Refused on an
    active rule."""
    rule = await _rule(session, rule_id)
    if rule.status == ACTIVE:
        raise _invalid(PAUSE_FIRST, rule)
    current = await _current_version(session, rule)
    new_params = (json_safe(validate_params(rule.model, params)) if params is not None
                  else current.params)
    if qty is not None or notional is not None:
        new_qty, new_notional = _size(qty, notional)
    else:
        new_qty, new_notional = current.qty, current.notional
    new_brakes = _brakes_json(brakes) if brakes is not None else current.brakes
    rule.version += 1
    session.add(AutoRuleVersion(rule_id=rule.id, version=rule.version, params=new_params,
                                qty=new_qty, notional=new_notional, brakes=new_brakes,
                                created_by=by[:64]))
    await session.flush()
    return rule


async def _check_activation(session: AsyncSession, rule: AutoRule,
                            portfolio: Portfolio) -> None:
    from smtrading.signals.service import approved_strategies  # heavy module, lazily

    ensure_not_archived(portfolio)
    if not portfolio.auto_rules_enabled:
        raise _invalid(SWITCH_OFF, rule)
    taken = await session.scalar(select(exists().where(
        AutoRule.portfolio_id == rule.portfolio_id, AutoRule.symbol == rule.symbol,
        AutoRule.status == ACTIVE, AutoRule.id != rule.id)))
    if taken:
        raise _invalid(ACTIVE_TAKEN, rule, symbol=rule.symbol)
    own_trade = exists().where(AutoTrade.rule_id == rule.id,
                               AutoTrade.status.in_((SENT, OPEN)))
    held = await session.scalar(select(exists().where(
        Position.portfolio_id == rule.portfolio_id, Position.symbol == rule.symbol,
        Position.qty != 0, ~own_trade)))
    if held:
        raise _invalid(POSITION_OPEN, rule, symbol=rule.symbol)
    ordered = await session.scalar(select(exists().where(
        Order.portfolio_id == rule.portfolio_id, Order.symbol == rule.symbol,
        Order.status.in_(OPEN_STATUSES), Order.auto_rule_id.is_distinct_from(rule.id))))
    if ordered:
        raise _invalid(ORDER_OPEN, rule, symbol=rule.symbol)
    if portfolio.intraday_enabled:
        for a in await approved_strategies(session):
            if a.timeframe in INTRADAY_TIMEFRAMES \
                    and rule.symbol in {canonical_symbol(s) for s in a.symbols} \
                    and any(x.id == portfolio.id for x in a.portfolios):
                raise _invalid(MODE_A_OWNS, rule, symbol=rule.symbol, strategy=a.ref)


async def _switch_on(session: AsyncSession, rule_id: uuid.UUID, now: datetime,
                     allowed: tuple[str, ...], refused: str) -> AutoRule:
    """→ active from one of `allowed`: the portfolio's lock, then the rule's (R11), then
    the checks; an active rule is returned as it is."""
    rule = await _rule(session, rule_id, lock=False)
    portfolio = await lock_portfolio(session, rule.portfolio_id)
    rule = await _rule(session, rule_id)
    if rule.status == ACTIVE:
        return rule
    if rule.status not in allowed:
        raise _invalid(refused, rule, status=rule.status)
    await _check_activation(session, rule, portfolio)
    _set_status(rule, ACTIVE, None, now)
    try:
        async with session.begin_nested():  # a concurrent activation: the index's word
            await session.flush()
    except IntegrityError:
        raise _invalid(ACTIVE_TAKEN, rule, symbol=rule.symbol) from None
    queue_track(session, rule.symbol, STREAM_SOURCE)
    return rule


async def activate(session: AsyncSession, rule_id: uuid.UUID, *, by: str,
                   now: datetime) -> AutoRule:
    """draft → active after the checks; the stream follows the symbol after the commit.
    An active rule is returned as it is."""
    return await _switch_on(session, rule_id, now, (DRAFT,), NOT_ACTIVATABLE)


async def resume(session: AsyncSession, rule_id: uuid.UUID, *, by: str,
                 now: datetime) -> AutoRule:
    """paused or braked → active, with the same checks as `activate`."""
    return await _switch_on(session, rule_id, now, (PAUSED, BRAKED), NOT_RESUMABLE)


async def _untrack_if_unused(session: AsyncSession, symbol: str) -> None:
    """After a rule left `active` (flushed): stop following the symbol for the rules if
    no other active rule, on any portfolio, has it."""
    still = await session.scalar(select(exists().where(
        AutoRule.symbol == symbol, AutoRule.status == ACTIVE)))
    if not still:
        queue_untrack(session, symbol, STREAM_SOURCE)


async def _leave(session: AsyncSession, rule: AutoRule, status: str,
                 reason: str | None) -> AutoRule:
    was_active = rule.status == ACTIVE
    _set_status(rule, status, reason, datetime.now(UTC))
    await session.flush()
    if was_active:
        await _untrack_if_unused(session, rule.symbol)
    return rule


async def pause(session: AsyncSession, rule_id: uuid.UUID, *, by: str,
                reason: str | None = None) -> AutoRule:
    """active or braked → paused (`status_reason` = reason); a paused rule stays as it
    is; a draft is refused."""
    rule = await _rule(session, rule_id)
    if rule.status == PAUSED:
        return rule
    if rule.status == DRAFT:
        raise _invalid(NOT_PAUSABLE, rule)
    return await _leave(session, rule, PAUSED, reason)


async def brake(session: AsyncSession, rule_id: uuid.UUID, reason: str) -> AutoRule:
    """active → braked with the brake's reason; any other status is left as it is."""
    rule = await _rule(session, rule_id)
    if rule.status != ACTIVE:
        return rule
    return await _leave(session, rule, BRAKED, reason)


async def pause_all(session: AsyncSession, portfolio_id: uuid.UUID, *, by: str,
                    reason: str) -> list[AutoRule]:
    """Every active or braked rule of the portfolio → paused (the switch turned off);
    drafts stay drafts. Returns the rules paused."""
    rules = list(await session.scalars(
        select(AutoRule).where(AutoRule.portfolio_id == portfolio_id,
                               AutoRule.status.in_((ACTIVE, BRAKED)))
        .order_by(AutoRule.id).with_for_update().execution_options(populate_existing=True)))
    for rule in rules:
        await _leave(session, rule, PAUSED, reason)
    return rules


async def auto_rule_owned_pairs(session: AsyncSession) -> frozenset[tuple[uuid.UUID, str]]:
    """The (portfolio, symbol) pairs of the active rules."""
    rows = await session.execute(
        select(AutoRule.portfolio_id, AutoRule.symbol).where(AutoRule.status == ACTIVE))
    return frozenset((pid, sym) for pid, sym in rows.all())


# --- the active rules (scalper) -----------------------------------------------------

@dataclass(frozen=True)
class ActiveRule:
    rule_id: uuid.UUID
    version: int
    portfolio_id: uuid.UUID
    symbol: str
    model: str
    params: dict
    qty: Decimal | None
    notional: Decimal | None
    brakes: Brakes
    portfolio_brakes: Brakes
    market: str
    asset_class: str
    tick_size: Decimal
    day_close_minutes: int
    base_currency: str


async def active_rules(session: AsyncSession) -> list[ActiveRule]:
    """The active rules of the non-archived portfolios with `auto_rules_enabled`, with
    their current version, portfolio and instrument: one query. A stored version that no
    longer validates is left out (logged)."""
    rows = await session.execute(
        select(AutoRule.id, AutoRule.version, AutoRule.portfolio_id, AutoRule.symbol,
               AutoRule.model, AutoRuleVersion.params, AutoRuleVersion.qty,
               AutoRuleVersion.notional, AutoRuleVersion.brakes, Portfolio.auto_brakes,
               Instrument.market, Instrument.asset_class, Instrument.tick_size,
               Portfolio.day_close_minutes, Portfolio.base_currency)
        .join(AutoRuleVersion, (AutoRuleVersion.rule_id == AutoRule.id)
              & (AutoRuleVersion.version == AutoRule.version))
        .join(Portfolio, Portfolio.id == AutoRule.portfolio_id)
        .join(Instrument, Instrument.symbol == AutoRule.symbol)
        .where(AutoRule.status == ACTIVE, Portfolio.auto_rules_enabled.is_(True),
               Portfolio.status != PortfolioStatus.archived)
        .order_by(AutoRule.created_at, AutoRule.id))
    out: list[ActiveRule] = []
    for (rid, version, pid, sym, model, params, qty, notional, brakes, pf_brakes, market,
         asset_class, tick, day_close, currency) in rows.all():
        try:
            out.append(ActiveRule(
                rule_id=rid, version=version, portfolio_id=pid, symbol=sym, model=model,
                params=validate_params(model, params), qty=qty, notional=notional,
                brakes=validate_brakes(brakes, level="rule"),
                portfolio_brakes=validate_brakes(pf_brakes or {}, level="portfolio"),
                market=market, asset_class=str(asset_class), tick_size=tick,
                day_close_minutes=day_close, base_currency=currency))
        except SMTError:
            log.warning("automatic rule %s version %s no longer validates: skipped",
                        rid, version)
    return out

