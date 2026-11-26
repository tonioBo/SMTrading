"""Admin API of intraday work (mode A: agent wake on a signal; mode B: automatic rules).
Admin only, never MCP: the agents see none of it.

    GET    /api/intraday/triggers?portfolio_id=&limit=   the trigger log, newest first
    GET    /api/intraday/accounts                        the broker account state per link
    DELETE /api/portfolios/{id}/intraday-triggers        end-of-drill cleanup (audited)

Also the checks of the intraday fields of PATCH /api/portfolios/{id} (routes_portfolios):
the field bounds, the agent to wake (permission `trade` on the portfolio, active), one mode
per portfolio (spec point 8), the day leverage (at most DAY_TRADE_MAX_LEVERAGE while a
switch is on) and, when the automatic rules are turned on, a fresh read of the broker
account with no transaction open: only a link of ACCOUNT_KINDS (every Alpaca account is a
margin account: the multiplier only caps the leverage, spec point 7), and not blocked.
Nothing here carries an account id or a secret.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from smtrading.admin.auth import CurrentAdmin, DbSession, RedisClient, require_admin
from smtrading.admin.common import _invalid, _transaction
from smtrading.agents.models import Agent, AgentPortfolio
from smtrading.agents.service import write_audit
from smtrading.brokers.models import BrokerLink
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.common.money import decimal_to_str
from smtrading.intraday.account_reader import read_account_state
from smtrading.intraday.day_trades import (
    ACCOUNT_KINDS,
    DAY_TRADE_MAX_LEVERAGE,
    day_trade_leverage,
    fresh_buying_power,
    own_cap_usage,
)
from smtrading.intraday.models import INTRADAY_TIMEFRAMES, IntradayTrigger, day_scope
from smtrading.mcp.dto import parse_int, to_jsonable
from smtrading.portfolios.models import Portfolio, PortfolioStatus

router = APIRouter(prefix="/api", tags=["intraday"], dependencies=[Depends(require_admin)])

DEFAULT_TRIGGER_LIMIT, MAX_TRIGGER_LIMIT = 50, 200
DAY_FIELDS = (
    "intraday_enabled", "auto_rules_enabled", "intraday_timeframes",
    "intraday_wake_agent_id", "day_close_minutes", "max_day_trades_5d",
    "intraday_max_wakes_per_hour",
)
_SWITCHES = ("intraday_enabled", "auto_rules_enabled")
DAY_CLOSE_MINUTES = (2, 60)
MAX_DAY_TRADES_5D = (0, 100)
WAKES_PER_HOUR = (1, 60)
WAKE_AGENT_MESSAGE = "l'agente da svegliare deve avere il permesso trade su questo portafoglio"
ONE_MODE_MESSAGE = (
    "Un portafoglio lavora in un solo modo: spegni il lavoro in giornata con l'agente prima "
    "di accendere le regole automatiche, o viceversa")
DAY_LEVERAGE_MESSAGE = (
    "Per il lavoro in giornata la leva del portafoglio può essere al massimo "
    f"{decimal_to_str(DAY_TRADE_MAX_LEVERAGE)}")
NO_ALPACA_MESSAGE = "Regole automatiche disponibili solo su portafogli collegati ad Alpaca"
READ_FAILED_MESSAGE = "Impossibile leggere il conto {link}: riprova"
BLOCKED_MESSAGE = ("Regole automatiche non attivabili: il conto {link} risulta bloccato "
                   "dal broker")
_STATE_FIELDS = ("buying_power", "multiplier", "equity", "trading_blocked",
                 "account_blocked", "read_at")
TRIGGER_FIELDS = (
    "id", "created_at", "portfolio_id", "strategy_ref", "strategy_name", "symbol", "rule",
    "timeframe", "bar_close", "value", "threshold", "price", "rule_text", "outcome",
    "issue_ident", "error",
)


# --- PATCH /api/portfolios/{id}: the intraday fields ------------------------------------------


def _bounded(value: Any, field: str, bounds: tuple[int, int], *, nullable: bool = False):
    if value is None:
        if nullable:
            return None
        raise _invalid(field, f"{field} non può essere nullo")
    lo, hi = bounds
    if not lo <= value <= hi:
        raise _invalid(field, f"{field} deve essere un intero tra {lo} e {hi}")
    return value


def _timeframes(value: list[str] | None) -> list[str]:
    allowed = ", ".join(INTRADAY_TIMEFRAMES)
    if (not value or len(set(value)) != len(value)
            or any(tf not in INTRADAY_TIMEFRAMES for tf in value)):
        raise _invalid("intraday_timeframes",
                       f"intraday_timeframes: uno o più tra {allowed}, senza doppioni")
    return list(value)


def day_values(body, fields: set[str]) -> dict[str, Any]:
    """The intraday fields sent, validated (the wake agent is checked by check_wake_agent):
    {field: new value}."""
    out: dict[str, Any] = {}
    for f in _SWITCHES:
        if f in fields:
            if getattr(body, f) is None:
                raise _invalid(f, f"{f} deve essere vero o falso")
            out[f] = getattr(body, f)
    if "intraday_timeframes" in fields:
        out["intraday_timeframes"] = _timeframes(body.intraday_timeframes)
    if "intraday_wake_agent_id" in fields:
        out["intraday_wake_agent_id"] = body.intraday_wake_agent_id
    if "day_close_minutes" in fields:
        out["day_close_minutes"] = _bounded(body.day_close_minutes, "day_close_minutes",
                                            DAY_CLOSE_MINUTES)
    if "max_day_trades_5d" in fields:
        out["max_day_trades_5d"] = _bounded(body.max_day_trades_5d, "max_day_trades_5d",
                                            MAX_DAY_TRADES_5D, nullable=True)
    if "intraday_max_wakes_per_hour" in fields:
        out["intraday_max_wakes_per_hour"] = _bounded(
            body.intraday_max_wakes_per_hour, "intraday_max_wakes_per_hour", WAKES_PER_HOUR)
    return out


def check_day_mode(intraday: bool, auto_rules: bool, leverage: Decimal,
                   leverage_checked: bool) -> None:
    """One mode per portfolio (spec point 8); with a switch on, the portfolio leverage at
    most DAY_TRADE_MAX_LEVERAGE when `leverage_checked` (a switch or the leverage sent)."""
    if intraday and auto_rules:
        raise _invalid("auto_rules_enabled", ONE_MODE_MESSAGE)
    if leverage_checked and (intraday or auto_rules) and leverage > DAY_TRADE_MAX_LEVERAGE:
        raise _invalid("leverage", DAY_LEVERAGE_MESSAGE)


async def check_wake_agent(session: AsyncSession, portfolio_id: uuid.UUID,
                           agent_id: uuid.UUID | None) -> None:
    """None clears it; else an active agent with the `trade` permission on the portfolio."""
    if agent_id is None:
        return
    found = await session.scalar(
        select(AgentPortfolio.agent_id)
        .join(Agent, Agent.id == AgentPortfolio.agent_id)
        .where(AgentPortfolio.agent_id == agent_id,
               AgentPortfolio.portfolio_id == portfolio_id,
               AgentPortfolio.permission == "trade", Agent.active.is_(True)))
    if found is None:
        raise _invalid("intraday_wake_agent_id", WAKE_AGENT_MESSAGE)


async def check_auto_rules_account(
    session: AsyncSession, sessionmaker: async_sessionmaker, portfolio: Portfolio
) -> uuid.UUID:
    """Before turning the automatic rules on: the portfolio's enabled link of
    ACCOUNT_KINDS, read now (the request's transaction is closed first: no transaction
    is open during the broker call), not blocked. The link id."""
    link = (await session.get(BrokerLink, portfolio.broker_link_id)
            if portfolio.broker_link_id else None)
    if link is None or not link.enabled or link.kind not in ACCOUNT_KINDS:
        raise _invalid("auto_rules_enabled", NO_ALPACA_MESSAGE)
    link_id, name = link.id, link.name
    await session.rollback()  # no transaction open during the broker read (R9)
    try:
        async with httpx.AsyncClient() as http:
            state = await read_account_state(sessionmaker, http, link_id)
    except SMTError:
        raise SMTError(ErrorCode.PROVIDER_UNAVAILABLE, READ_FAILED_MESSAGE.format(link=name),
                       {"field": "auto_rules_enabled", "broker_link_id": str(link_id)},
                       ) from None
    if state.get("trading_blocked") or state.get("account_blocked"):
        raise _invalid("auto_rules_enabled", BLOCKED_MESSAGE.format(link=name))
    return link_id


async def audit_day_changes(session: AsyncSession, portfolio_id: uuid.UUID,
                            changes: dict[str, Any], by: str) -> None:
    await write_audit(
        session, agent_id=None, method="admin:portfolio.intraday",
        params={"portfolio_id": portfolio_id, "changes": changes, "by": by},
        outcome="ok", latency_ms=0,
    )


# --- routes ------------------------------------------------------------------------------


@router.get("/intraday/triggers")
async def list_triggers(
    session: DbSession, portfolio_id: uuid.UUID | None = None,
    limit: int = DEFAULT_TRIGGER_LIMIT,
) -> list[dict]:
    n = parse_int(limit, "limit", 1, MAX_TRIGGER_LIMIT)
    q = (select(IntradayTrigger, Portfolio.name)
         .join(Portfolio, Portfolio.id == IntradayTrigger.portfolio_id)
         .order_by(IntradayTrigger.created_at.desc(), IntradayTrigger.id.desc()).limit(n))
    if portfolio_id is not None:
        q = q.where(IntradayTrigger.portfolio_id == portfolio_id)
    out = []
    for t, name in (await session.execute(q)).all():
        row = {f: to_jsonable(getattr(t, f)) for f in TRIGGER_FIELDS}
        row["portfolio_name"] = name
        out.append(row)
    return out


def _margin(multiplier: Any) -> bool:
    try:
        return multiplier is not None and Decimal(str(multiplier)) > 1
    except (InvalidOperation, ValueError):
        return False


@router.get("/intraday/accounts")
async def list_accounts(session: DbSession) -> list[dict]:
    """Per broker link with at least one non-archived portfolio: the account state last
    read (`fresh`: read within ACCOUNT_STATE_MAX_AGE), and per portfolio its switches,
    leverage, day leverage and own day-trade cap usage (only in day scope, else null)."""
    now = datetime.now(UTC)
    portfolios = list(await session.scalars(
        select(Portfolio).where(Portfolio.broker_link_id.is_not(None),
                                Portfolio.status != PortfolioStatus.archived)
        .order_by(Portfolio.name)))
    by_link: dict[uuid.UUID, list[Portfolio]] = {}
    for p in portfolios:
        by_link.setdefault(p.broker_link_id, []).append(p)  # type: ignore[arg-type]
    if not by_link:
        return []
    links = list(await session.scalars(
        select(BrokerLink).where(BrokerLink.id.in_(list(by_link))).order_by(BrokerLink.name)))
    out = []
    for link in links:
        state = link.pdt_state if isinstance(link.pdt_state, dict) else {}
        rows = []
        for p in by_link[link.id]:
            usage = await own_cap_usage(session, p, now) if day_scope(p) else None
            rows.append({
                "portfolio_id": p.id, "portfolio_name": p.name,
                "intraday_enabled": p.intraday_enabled,
                "auto_rules_enabled": p.auto_rules_enabled, "leverage": p.leverage,
                "day_leverage": day_trade_leverage(p, link),
                "max_day_trades_5d": p.max_day_trades_5d,
                "used": usage.used if usage else None,
                "pending": usage.pending if usage else None,
                "remaining": usage.remaining if usage else None,
                "frees_on": usage.frees_on if usage else None,
            })
        out.append({
            "link_id": link.id, "link_name": link.name, "kind": link.kind,
            **{f: state.get(f) for f in _STATE_FIELDS},
            "fresh": fresh_buying_power(link, now) is not None,
            "margin": _margin(state.get("multiplier")),
            "portfolios": rows,
        })
    return to_jsonable(out)


@router.delete("/portfolios/{portfolio_id}/intraday-triggers")
async def delete_triggers(
    portfolio_id: uuid.UUID, admin: CurrentAdmin, session: DbSession, redis: RedisClient
) -> dict:
    """The end-of-drill cleanup: every trigger of the portfolio."""
    by = admin.email
    async with _transaction(session, redis):
        if await session.get(Portfolio, portfolio_id) is None:
            raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found",
                           {"portfolio_id": str(portfolio_id)})
        result = await session.execute(
            delete(IntradayTrigger).where(IntradayTrigger.portfolio_id == portfolio_id))
        deleted = result.rowcount or 0
        await write_audit(
            session, agent_id=None, method="admin:intraday.triggers_delete",
            params={"portfolio_id": portfolio_id, "deleted": deleted, "by": by},
            outcome="ok", latency_ms=0,
        )
    return {"deleted": deleted}
