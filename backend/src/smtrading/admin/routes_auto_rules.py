"""Admin API of the automatic rules (intraday mode B). Admin only (viewers get 403 on
every route, reads included), never MCP: the agents see none of it.

    GET    /api/auto-rules/models                  the models and their parameter bounds
    GET    /api/auto-rules?portfolio_id=           the rules with their current version
    POST   /api/auto-rules                         a new rule, as a draft
    PATCH  /api/auto-rules/{id}                    a new version (refused while active)
    POST   /api/auto-rules/{id}/activate|pause|resume
    GET    /api/auto-rules/{id}/trades?limit=      its trades, newest first
    GET    /api/auto-rules/results?portfolio_id=&from=&to=   per rule and per UTC day
    POST   /api/auto-rules/backtest                the replay on one-minute history
    DELETE /api/portfolios/{id}/auto-rules         end-of-drill cleanup of a test portfolio

Every write is audited as `admin:auto_rules.<action>` with the admin's email (`by`); the
service (`autorules.rules`) gives the Italian refusals. Decimal parameters and brakes
travel as strings (ints accepted): a JSON number with a fraction is refused with a clear
message, never read through float. Numbers go out as strings (`to_jsonable`). Nothing
here carries an account id or a secret: the broker link appears only as its id."""

import asyncio
import functools
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.admin.schemas import (
    AutoBacktestIn,
    AutoRuleCreateIn,
    AutoRulePatchIn,
    AutoRulePauseIn,
)
from smtrading.agents.service import write_audit
from smtrading.autorules import rules as service
from smtrading.autorules.brakes import day_start
from smtrading.autorules.feed import read_stream_status, stream_state
from smtrading.autorules.guards import BLOCKED_KEY, BLOCKED_TTL_S
from smtrading.autorules.models import model_catalog, validate_brakes, validate_params
from smtrading.autorules.replay import ReplayResult, max_drawdown, replay
from smtrading.autorules.tables import (
    CLOSED,
    OPEN,
    SENT,
    STATUS_IT,
    AutoRule,
    AutoRuleVersion,
    AutoTrade,
)
from smtrading.backtest.runner import zero_fee_rule
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.events import json_safe
from smtrading.fees.broker_presets import preset_for
from smtrading.fees.calculator import FeeRuleData
from smtrading.fees.models import FeeScope, FeeSide
from smtrading.fees.resolver import load_rules, resolve_rule
from smtrading.instruments.models import Instrument
from smtrading.instruments.service import canonical_symbol
from smtrading.marketdata.types import Timeframe
from smtrading.mcp.dto import to_jsonable
from smtrading.portfolios.models import Portfolio

router = APIRouter(prefix="/api", tags=["auto-rules"], dependencies=[Depends(require_admin)])

STREAM_IT = {"stream": "con flusso", "polling": "in polling", "none": "senza flusso"}
DEFAULT_TRADE_LIMIT, MAX_TRADE_LIMIT = 100, 500
DEFAULT_RESULT_DAYS, MAX_RESULT_DAYS = 30, 92
BACKTEST_MAX_DAYS, BACKTEST_MAX_SYMBOLS = 30, 5
MAX_RETURNED_TRADES = 500  # the newest ones; the totals count them all
BACKTEST_BUSY = "una prova sullo storico è già in corso: riprova tra poco"
# one backtest at a time per API worker (the replay is CPU work, run in a thread)
_backtest_slot = asyncio.Semaphore(1)
MAX_SPREAD_PCT, MAX_DELAY_S = Decimal(5), 60
DEFAULT_DAY_CLOSE_MINUTES = 10  # the portfolios' column default, when no portfolio is given
FLOAT_MESSAGE = '{label} {key}: scrivi i numeri decimali come testo, per esempio "0.3"'
NOT_FOUND = "regola non trovata"


# --- input --------------------------------------------------------------------------


def decimals_as_text(values: dict[str, Any], label: str) -> dict[str, Any]:
    """`values` unchanged; a JSON number with a fraction (a float) is refused: decimals
    travel as strings and are never read through float."""
    for k, v in values.items():
        if isinstance(v, float):
            raise SMTError(ErrorCode.INVALID_ORDER, FLOAT_MESSAGE.format(label=label, key=k),
                           {"param": k})
    return values


def portfolio_brakes_json(value: dict[str, Any] | None) -> dict:
    """The portfolio's brakes for `portfolios.auto_brakes`: validated (level "portfolio",
    the defaults filled in), JSON-safe. None or {}: the defaults."""
    brakes = validate_brakes(decimals_as_text(value or {}, "freno"), level="portfolio")
    return json_safe(asdict(brakes))


def _dec(value: str | int | None, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except InvalidOperation:
        raise _invalid(field, f"{field}: atteso un numero") from None
    if not d.is_finite():
        raise _invalid(field, f"{field}: atteso un numero")
    return d


def _day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid(field, f"{field}: data non valida (AAAA-MM-GG)") from None


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=UTC)


def _when(value: str, field: str, *, end: bool) -> datetime:
    """YYYY-MM-DD: midnight UTC of the day (of the next day for `end`, the day included);
    else an ISO 8601 time (naive: UTC)."""
    if len(value.strip()) == 10:
        d = _day(value.strip(), field)
        return _midnight(d + timedelta(days=1) if end else d)
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        raise _invalid(field, f"{field}: data non valida (ISO 8601)") from None
    return ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)


# --- the rule DTO -------------------------------------------------------------------


async def _stream_status(redis) -> dict | None:
    try:
        return await read_stream_status(redis)
    except RedisError:
        return None  # read as «senza flusso»


async def _rule_dtos(session: AsyncSession, redis, *, portfolio_id: uuid.UUID | None = None,
                     rule_id: uuid.UUID | None = None) -> list[dict]:
    status = await _stream_status(redis)  # before any query
    q = (select(AutoRule, AutoRuleVersion, Portfolio.name, Portfolio.timezone,
                Portfolio.base_currency)
         .join(AutoRuleVersion, (AutoRuleVersion.rule_id == AutoRule.id)
               & (AutoRuleVersion.version == AutoRule.version))
         .join(Portfolio, Portfolio.id == AutoRule.portfolio_id)
         .order_by(Portfolio.name, AutoRule.symbol, AutoRule.created_at, AutoRule.id))
    if portfolio_id is not None:
        q = q.where(AutoRule.portfolio_id == portfolio_id)
    if rule_id is not None:
        q = q.where(AutoRule.id == rule_id)
    rows = (await session.execute(q)).all()
    ids = [r.id for r, *_ in rows]
    if not ids:
        return []
    busy = set(await session.scalars(
        select(AutoTrade.rule_id).where(AutoTrade.rule_id.in_(ids),
                                        AutoTrade.status.in_((SENT, OPEN))).distinct()))
    now = datetime.now(UTC)
    closed = (await session.execute(
        select(AutoTrade.rule_id, AutoTrade.net_result, AutoTrade.exit_filled_at)
        .where(AutoTrade.rule_id.in_(ids), AutoTrade.status == CLOSED,
               AutoTrade.net_result.is_not(None),
               AutoTrade.exit_filled_at >= now - timedelta(days=2)))).all()
    out = []
    for rule, version, name, tz, currency in rows:
        start = day_start(tz, now)  # the portfolio's day, as the brakes count it
        nets = [net for rid, net, at in closed if rid == rule.id and at >= start]
        stream = stream_state(status, rule.symbol)
        out.append(to_jsonable({
            "id": rule.id, "portfolio_id": rule.portfolio_id, "portfolio_name": name,
            "symbol": rule.symbol, "model": rule.model, "status": rule.status,
            "status_it": STATUS_IT.get(rule.status, rule.status),
            "status_reason": rule.status_reason, "status_changed_at": rule.status_changed_at,
            "version": rule.version, "params": version.params, "qty": version.qty,
            "notional": version.notional, "brakes": version.brakes,
            "stream": stream, "stream_it": STREAM_IT[stream],
            "open_trade": rule.id in busy,
            "today": {"trades": len(nets), "net": sum(nets, Decimal(0)),
                      "net_currency": currency},
        }))
    return out


async def _one(session: AsyncSession, redis, rule_id: uuid.UUID) -> dict:
    found = await _rule_dtos(session, redis, rule_id=rule_id)
    if not found:
        raise SMTError(ErrorCode.NOT_FOUND, NOT_FOUND, {"rule_id": str(rule_id)})
    return found[0]


async def _audit(session: AsyncSession, action: str, rule: AutoRule, by: str,
                 **extra: Any) -> None:
    await write_audit(
        session, agent_id=None, method=f"admin:auto_rules.{action}",
        params={"rule_id": rule.id, "portfolio_id": rule.portfolio_id,
                "symbol": rule.symbol, "model": rule.model, "version": rule.version,
                "status": rule.status, **extra, "by": by},
        outcome="ok", latency_ms=0,
    )


# --- reads --------------------------------------------------------------------------


@router.get("/auto-rules/models")
async def models() -> list[dict]:
    return to_jsonable(model_catalog())


@router.get("/auto-rules")
async def list_rules(session: DbSession, redis: RedisClient,
                     portfolio_id: uuid.UUID | None = None) -> list[dict]:
    return await _rule_dtos(session, redis, portfolio_id=portfolio_id)


def _median(values: list[int]) -> Decimal | None:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return Decimal(v[mid]) if len(v) % 2 else (Decimal(v[mid - 1]) + Decimal(v[mid])) / 2


def _day_result(rule_id: uuid.UUID, version: int, day: date, trades: list[AutoTrade],
                blocked: dict[str, int]) -> dict:
    closed = [t for t in trades if t.status == CLOSED and t.net_result is not None]
    fees: dict[str, Decimal] = {}
    for t in closed:
        if t.fees and t.fees_currency:
            fees[t.fees_currency] = fees.get(t.fees_currency, Decimal(0)) + t.fees
    slips = [t.entry_slippage_bps for t in trades if t.entry_slippage_bps is not None]
    return {
        "rule_id": rule_id, "version": max((t.rule_version for t in trades), default=version),
        "day": day, "trades": len(closed),
        "wins": sum(1 for t in closed if t.net_result > 0),
        "losses": sum(1 for t in closed if t.net_result < 0),
        "net": sum((t.net_result for t in closed), Decimal(0)),
        "net_currency": next((t.net_currency for t in closed if t.net_currency), None),
        "fees": fees,
        "median_ms_trigger_to_send": _median(
            [t.ms_trigger_to_send for t in trades if t.ms_trigger_to_send is not None]),
        "median_ms_send_to_broker": _median(
            [t.ms_send_to_broker for t in trades if t.ms_send_to_broker is not None]),
        "median_ms_broker_to_fill": _median(
            [t.ms_broker_to_fill for t in trades if t.ms_broker_to_fill is not None]),
        "avg_entry_slippage_bps": (sum(slips, Decimal(0)) / len(slips)).quantize(
            Decimal("0.01")) if slips else None,
        "blocked": blocked,
    }


def _text(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


@router.get("/auto-rules/results")
async def results(
    session: DbSession, redis: RedisClient, portfolio_id: uuid.UUID | None = None,
    from_: str | None = Query(None, alias="from"), to: str | None = None,
) -> list[dict]:
    """Per rule and per UTC day (the day of the blocked-entry counters and of the evening
    summary): the trades triggered that day (wins, losses, net and fees of the closed
    ones; medians and mean slippage over all), and the entries the guards blocked.
    `from`/`to`: YYYY-MM-DD, both included (default: the last DEFAULT_RESULT_DAYS days)."""
    last = _day(to, "to") if to else datetime.now(UTC).date()
    first = _day(from_, "from") if from_ else last - timedelta(days=DEFAULT_RESULT_DAYS - 1)
    if first > last:
        raise _invalid("from", "from deve essere prima di to")
    if (last - first).days + 1 > MAX_RESULT_DAYS:
        raise _invalid("from", f"al massimo {MAX_RESULT_DAYS} giorni")
    q = select(AutoRule.id, AutoRule.version)
    if portfolio_id is not None:
        q = q.where(AutoRule.portfolio_id == portfolio_id)
    versions: dict[uuid.UUID, int] = dict((await session.execute(q)).all())
    if not versions:
        return []
    trades = list(await session.scalars(
        select(AutoTrade).where(AutoTrade.rule_id.in_(list(versions)),
                                AutoTrade.trigger_at >= _midnight(first),
                                AutoTrade.trigger_at < _midnight(last + timedelta(days=1)))
        .order_by(AutoTrade.trigger_at, AutoTrade.id)))
    session.expunge_all()  # the rows stay loaded (a rollback would expire them)
    await session.rollback()  # no transaction open across the Redis reads
    # the counters expire after BLOCKED_TTL_S: older days are not looked up
    oldest = datetime.now(UTC).date() - timedelta(seconds=BLOCKED_TTL_S)
    days = [d for d in (first + timedelta(days=i) for i in range((last - first).days + 1))
            if d >= oldest]
    keys = [(rid, d) for rid in versions for d in days]
    async with redis.pipeline(transaction=False) as pipe:
        for rid, d in keys:
            pipe.hgetall(BLOCKED_KEY.format(rule_id=rid, day=f"{d:%Y%m%d}"))
        counters = await pipe.execute()
    groups: dict[tuple[uuid.UUID, date], list[AutoTrade]] = {}
    for t in trades:
        groups.setdefault((t.rule_id, t.trigger_at.astimezone(UTC).date()), []).append(t)
    blocked = {key: {_text(k): int(v) for k, v in c.items()}
               for key, c in zip(keys, counters, strict=True) if c}
    out = [_day_result(rid, versions[rid], d, groups.get((rid, d), []), blocked.get((rid, d), {}))
           for rid, d in sorted(set(groups) | set(blocked), key=lambda k: (k[1], str(k[0])))]
    return to_jsonable(out)


@router.get("/auto-rules/{rule_id}/trades")
async def trades(rule_id: uuid.UUID, session: DbSession,
                 limit: int = DEFAULT_TRADE_LIMIT) -> list[dict]:
    if not 1 <= limit <= MAX_TRADE_LIMIT:
        raise _invalid("limit", f"limit deve essere un intero tra 1 e {MAX_TRADE_LIMIT}")
    if await session.get(AutoRule, rule_id) is None:
        raise SMTError(ErrorCode.NOT_FOUND, NOT_FOUND, {"rule_id": str(rule_id)})
    rows = await session.scalars(
        select(AutoTrade).where(AutoTrade.rule_id == rule_id)
        .order_by(AutoTrade.trigger_at.desc(), AutoTrade.id.desc()).limit(limit))
    fields = [a.key for a in AutoTrade.__mapper__.column_attrs]
    return [to_jsonable({f: getattr(t, f) for f in fields}) for t in rows]


# --- writes -------------------------------------------------------------------------


@router.post("/auto-rules")
async def create(body: AutoRuleCreateIn, admin: CurrentAdmin, session: DbSession,
                 redis: RedisClient) -> dict:
    by = admin.email
    params = decimals_as_text(body.params, "parametro")
    brakes = None if body.brakes is None else decimals_as_text(body.brakes, "freno")
    async with _transaction(session, redis):
        rule = await service.create_rule(
            session, portfolio_id=body.portfolio_id, symbol=body.symbol, model=body.model,
            params=params, qty=_dec(body.qty, "qty"), notional=_dec(body.notional, "notional"),
            brakes=brakes, by=by)
        await _audit(session, "create", rule, by)
    return await _one(session, redis, rule.id)


@router.patch("/auto-rules/{rule_id}")
async def update(rule_id: uuid.UUID, body: AutoRulePatchIn, admin: CurrentAdmin,
                 session: DbSession, redis: RedisClient) -> dict:
    by = admin.email
    params = None if body.params is None else decimals_as_text(body.params, "parametro")
    brakes = None if body.brakes is None else decimals_as_text(body.brakes, "freno")
    async with _transaction(session, redis):
        rule = await service.update_rule(
            session, rule_id, params=params, qty=_dec(body.qty, "qty"),
            notional=_dec(body.notional, "notional"), brakes=brakes, by=by)
        await _audit(session, "update", rule, by,
                     changed=sorted(body.model_fields_set))
    return await _one(session, redis, rule_id)


@router.post("/auto-rules/{rule_id}/activate")
async def activate(rule_id: uuid.UUID, admin: CurrentAdmin, session: DbSession,
                   redis: RedisClient) -> dict:
    by = admin.email
    async with _transaction(session, redis):
        rule = await service.activate(session, rule_id, by=by, now=datetime.now(UTC))
        await _audit(session, "activate", rule, by)
    return await _one(session, redis, rule_id)


@router.post("/auto-rules/{rule_id}/pause")
async def pause(rule_id: uuid.UUID, admin: CurrentAdmin, session: DbSession,
                redis: RedisClient, body: AutoRulePauseIn | None = None) -> dict:
    by = admin.email
    reason = None
    if body is not None and body.reason is not None:
        reason = body.reason.strip() or None
    async with _transaction(session, redis):
        rule = await service.pause(session, rule_id, by=by, reason=reason)
        await _audit(session, "pause", rule, by, reason=reason)
    return await _one(session, redis, rule_id)


@router.post("/auto-rules/{rule_id}/resume")
async def resume(rule_id: uuid.UUID, admin: CurrentAdmin, session: DbSession,
                 redis: RedisClient) -> dict:
    """Paused or stopped by the brakes → active (the brakes' reset is a resume)."""
    by = admin.email
    async with _transaction(session, redis):
        rule = await service.resume(session, rule_id, by=by, now=datetime.now(UTC))
        await _audit(session, "resume", rule, by)
    return await _one(session, redis, rule_id)


@router.delete("/portfolios/{portfolio_id}/auto-rules")
async def purge(portfolio_id: uuid.UUID, admin: CurrentAdmin, session: DbSession,
                redis: RedisClient) -> dict:
    """End of a drill on a test portfolio without agents (autorules.rules.purge_portfolio)."""
    by = admin.email
    async with _transaction(session, redis):
        out = await service.purge_portfolio(session, portfolio_id, by=by)
    return out


# --- backtest -----------------------------------------------------------------------

BROKER_NEEDS_REF = ("per le commissioni del broker indica il portafoglio o il collegamento "
                    "(uno solo)")
NO_PRESET = "nessun listino commissioni per il broker di questo collegamento"
NO_FEE_RULE = "nessuna regola di commissione del portafoglio per {cls} ({side})"


def _limit(message: str, **details: Any) -> SMTError:
    return SMTError(ErrorCode.RISK_LIMIT_EXCEEDED, message, details)


def _backtest_symbols(body: AutoBacktestIn) -> list[str]:
    if (body.symbol is None) == (body.symbols is None):
        raise _invalid("symbol", "indica il simbolo oppure l'elenco dei simboli")
    raw = [body.symbol] if body.symbol is not None else list(body.symbols or [])
    symbols = list(dict.fromkeys(canonical_symbol(s) for s in raw if s.strip()))
    if not symbols:
        raise _invalid("symbols", "indica almeno un simbolo")
    if len(symbols) > BACKTEST_MAX_SYMBOLS:
        raise _limit(f"al massimo {BACKTEST_MAX_SYMBOLS} simboli per prova",
                     limit_symbols=BACKTEST_MAX_SYMBOLS, symbols=len(symbols))
    return symbols


def _backtest_span(body: AutoBacktestIn) -> tuple[datetime, datetime]:
    start = _when(body.from_, "from", end=False)
    end = min(_when(body.to, "to", end=True), datetime.now(UTC))
    if start >= end:
        raise _invalid("to", "to deve essere dopo from (e non nel futuro)")
    if end - start > timedelta(days=BACKTEST_MAX_DAYS):
        raise _limit(f"al massimo {BACKTEST_MAX_DAYS} giorni di storico al minuto per prova",
                     limit_days=BACKTEST_MAX_DAYS)
    return start, end


def _preset_rule(link: BrokerLink, instr: Instrument) -> FeeRuleData:
    """The link's broker preset entry for the instrument's class (else the entry for every
    class), as a rule on both sides in the instrument's currency."""
    preset = preset_for(link.kind)
    if preset is None:
        raise _invalid("broker_link_id", NO_PRESET)
    cls = str(instr.asset_class)
    entry = (next((e for e in preset.entries if e.asset_class == cls), None)
             or next((e for e in preset.entries if e.asset_class is None), None))
    if entry is None:
        raise _invalid("broker_link_id", NO_PRESET)
    return FeeRuleData(
        id=uuid.uuid4(), scope=FeeScope.global_, scope_ref=None, side=FeeSide.both,
        currency=instr.currency, fixed=entry.fixed, pct=entry.pct, min_fee=None,
        max_fee=None, tiers=None, fx_pct=entry.fx_pct, priority=0, active=True,
        created_at=datetime.now(UTC))


async def _fee_rules(session: AsyncSession, body: AutoBacktestIn,
                     instruments: dict[str, Instrument]
                     ) -> dict[str, tuple[FeeRuleData, FeeRuleData]]:
    """Per symbol the (buy, sell) fee rules: zero, the portfolio's rules resolved per side
    (the existing fee rules), or the link's broker preset."""
    if body.fee_rule == "zero":
        zero = zero_fee_rule()
        return {s: (zero, zero) for s in instruments}
    if (body.portfolio_id is None) == (body.broker_link_id is None):
        raise _invalid("fee_rule", BROKER_NEEDS_REF)
    if body.broker_link_id is not None:
        link = await session.get(BrokerLink, body.broker_link_id)
        if link is None:
            raise SMTError(ErrorCode.NOT_FOUND, "collegamento non trovato",
                           {"broker_link_id": str(body.broker_link_id)})
        out = {}
        for s, instr in instruments.items():
            rule = _preset_rule(link, instr)
            out[s] = (rule, rule)
        return out
    rules = await load_rules(session)
    out = {}
    for s, instr in instruments.items():
        sides = []
        for side in ("buy", "sell"):
            try:
                sides.append(resolve_rule(rules, portfolio_id=body.portfolio_id,  # type: ignore[arg-type]
                                          asset_class=instr.asset_class, side=side))
            except SMTError as e:
                if e.code != ErrorCode.FEE_RULE_NOT_FOUND:
                    raise
                raise SMTError(ErrorCode.FEE_RULE_NOT_FOUND, NO_FEE_RULE.format(
                    cls=instr.asset_class.value, side=side), e.details) from None
        out[s] = (sides[0], sides[1])
    return out


def _result_dto(results: dict[str, ReplayResult], start: datetime, end: datetime) -> dict:
    """The totals over every trade; `trades` holds at most the MAX_RETURNED_TRADES newest
    (by exit time), `truncated` says when some were left out."""
    trades = sorted(((s, t) for s, r in results.items() for t in r.trades),
                    key=lambda x: (x[1].exit_ts, x[0]))
    nets = [t.net for _, t in trades]
    notes = list(dict.fromkeys(n for r in results.values() for n in r.notes))
    shown = trades[-MAX_RETURNED_TRADES:]
    return to_jsonable({
        "symbols": list(results), "from": start, "to": end,
        "trades": [{"symbol": s, **asdict(t)} for s, t in shown],
        "trades_total": len(trades), "truncated": len(shown) < len(trades),
        "net": sum(nets, Decimal(0)),
        "fees": sum((t.fees for _, t in trades), Decimal(0)),
        "wins": sum(r.wins for r in results.values()),
        "losses": sum(r.losses for r in results.values()),
        "max_drawdown": max_drawdown(nets),
        "notes": notes,
        "per_symbol": [{"symbol": s, "trades": len(r.trades), "net": r.net, "fees": r.fees,
                        "wins": r.wins, "losses": r.losses, "max_drawdown": r.max_drawdown}
                       for s, r in results.items()],
    })


@router.post("/auto-rules/backtest")
async def backtest(body: AutoBacktestIn, request: Request, session: DbSession) -> dict:
    """The replay (autorules.replay) of a model on one-minute bars (MarketDataRouter,
    shared cache and rate limit): at most BACKTEST_MAX_DAYS days and BACKTEST_MAX_SYMBOLS
    symbols (RISK_LIMIT_EXCEEDED beyond). The reads come first, then the transaction is
    closed: no transaction is open during the history fetch. The replay and the result run
    in a worker thread (the event loop keeps serving), one backtest at a time per worker:
    another one meanwhile is refused (RATE_LIMITED)."""
    if _backtest_slot.locked():
        raise SMTError(ErrorCode.RATE_LIMITED, BACKTEST_BUSY)
    async with _backtest_slot:
        return await _backtest(body, request, session)


async def _backtest(body: AutoBacktestIn, request: Request, session: AsyncSession) -> dict:
    symbols = _backtest_symbols(body)
    start, end = _backtest_span(body)
    params = validate_params(body.model, decimals_as_text(body.params, "parametro"))
    qty, notional = _dec(body.qty, "qty"), _dec(body.notional, "notional")
    size = qty if qty is not None else notional
    if (qty is None) == (notional is None) or size is None or not size > 0:
        raise _invalid("qty", service.SIZE_MESSAGE)
    spread = _dec(body.spread_pct, "spread_pct")
    if spread is None or not 0 <= spread <= MAX_SPREAD_PCT:
        raise _invalid("spread_pct", f"spread_pct deve essere tra 0 e {MAX_SPREAD_PCT}")
    if not 0 <= body.delay_s <= MAX_DELAY_S:
        raise _invalid("delay_s", f"delay_s deve essere tra 0 e {MAX_DELAY_S} secondi")
    instruments: dict[str, Instrument] = {}
    for s in symbols:
        instr = await session.get(Instrument, s)
        if instr is None or not instr.active or instr.asset_class not in service.ALLOWED_CLASSES:
            raise _invalid("symbol", f"{service.ONLY_US_CRYPTO}: {s}")
        instruments[s] = instr
    day_close = DEFAULT_DAY_CLOSE_MINUTES
    if body.portfolio_id is not None:
        portfolio = await session.get(Portfolio, body.portfolio_id)
        if portfolio is None:
            raise SMTError(ErrorCode.NOT_FOUND, "portafoglio non trovato",
                           {"portfolio_id": str(body.portfolio_id)})
        day_close = portfolio.day_close_minutes
    fees = await _fee_rules(session, body, instruments)
    session.expunge_all()  # the instruments stay loaded (a rollback would expire them)
    await session.rollback()  # no transaction open during the history fetch (R9)
    md = request.app.state.mcp_runtime.router
    if md is None:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, "dati di mercato non disponibili")
    results: dict[str, ReplayResult] = {}
    for s, instr in instruments.items():
        bars = await md.get_bars(s, Timeframe.m1, start, end)
        buy, sell = fees[s]
        results[s] = await asyncio.to_thread(functools.partial(
            replay, bars, model=body.model, params=params, market=instr.market,
            tick_size=instr.tick_size, qty=qty, notional=notional, fee_rule=buy,
            exit_fee_rule=sell, spread_pct=spread, delay_s=body.delay_s,
            day_close_minutes=day_close, lot_size=instr.lot_size))
    return await asyncio.to_thread(_result_dto, results, start, end)
