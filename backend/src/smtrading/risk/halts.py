"""«Blocco aperture» (trading halt): set by the CEO agent (MCP, `can_halt`) or an admin.

A halt is `global` (every portfolio) or on one `portfolio`. While one covers a portfolio,
an order that would increase exposure there is refused with TRADING_HALTED, at pre-trade
(stored as a rejection) and again at fill / before sending to a broker, with the kill
switch's exposure test: only orders that strictly reduce the position pass (closes,
bracket exits, reduce_only orders). Broker fills, liquidations and cancels are untouched.

At most one active halt per target (partial unique indexes); setting one again with the
same reason changes nothing, with another reason it replaces it (the old row is cleared by
the same author). Rows are never deleted: the cleared ones are the history. Every change
records a direct critical activity (`halt.set` / `halt.cleared`) that SMTrading sends to
Telegram itself. Set/clear serialise on a transaction-level advisory lock.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.common.errors import ErrorCode, SMTError
from smtrading.events import queue_event
from smtrading.portfolios.models import Portfolio
from smtrading.risk.models import HaltScope, TradingHalt
from smtrading.telegram.render import clean_text

MAX_REASON = 500
MESSAGE_PREFIX = "Aperture sospese dal CEO: "
SCOPES = tuple(s.value for s in HaltScope)
_LOCK_KEY = "smtrading.trading_halts"


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def clean_reason(value: Any, *, required: bool = True) -> str | None:
    """1-500 characters once control characters are removed and the ends stripped."""
    if value is None and not required:
        return None
    reason = clean_text(value) if isinstance(value, str) else ""
    if not reason:
        if not required:
            return None
        raise _invalid("reason", "reason is required")
    if len(reason) > MAX_REASON:
        raise _invalid("reason", f"reason must be at most {MAX_REASON} characters")
    return reason


def halt_message(halt: TradingHalt) -> str:
    return MESSAGE_PREFIX + halt.reason


async def _target(
    session: AsyncSession, scope: Any, portfolio_id: uuid.UUID | None
) -> tuple[str, uuid.UUID | None]:
    if scope not in SCOPES:
        raise _invalid("scope", "scope must be one of " + ", ".join(SCOPES))
    if scope == HaltScope.global_:
        if portfolio_id is not None:
            raise _invalid("portfolio_id", "portfolio_id is only for scope portfolio")
        return scope, None
    if portfolio_id is None:
        raise _invalid("portfolio_id", "portfolio_id is required for scope portfolio")
    if await session.get(Portfolio, portfolio_id) is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found",
                       {"portfolio_id": str(portfolio_id)})
    return scope, portfolio_id


def _target_filter(scope: str, portfolio_id: uuid.UUID | None):
    if scope == HaltScope.global_:
        return TradingHalt.scope == scope
    return (TradingHalt.scope == scope) & (TradingHalt.portfolio_id == portfolio_id)


async def _lock(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_LOCK_KEY))))


async def _active_for(session: AsyncSession, scope: str, portfolio_id) -> TradingHalt | None:
    return await session.scalar(
        select(TradingHalt).where(TradingHalt.active, _target_filter(scope, portfolio_id))
    )


def _event(session: AsyncSession, type_: str, halt: TradingHalt, by: str,
           agent_id: uuid.UUID | None, reason: str | None) -> None:
    queue_event(session, type_, {
        "halt_id": halt.id, "scope": halt.scope, "portfolio_id": halt.portfolio_id,
        # the activity data reaches agents (feed): an admin is named without the email
        "agent_id": agent_id, "reason": reason,
        "by": "admin" if by.startswith("admin:") else by,
    })


def _close(halt: TradingHalt, by: str) -> None:
    halt.active = False
    halt.cleared_by = by
    halt.cleared_at = datetime.now(UTC)


async def set_halt(
    session: AsyncSession, *, scope: Any, portfolio_id: uuid.UUID | None, reason: Any,
    by: str, agent_id: uuid.UUID | None = None,
) -> tuple[TradingHalt, bool]:
    """Activate a halt; returns (the active halt, whether anything changed)."""
    reason_ = clean_reason(reason)
    scope_, pid = await _target(session, scope, portfolio_id)
    await _lock(session)
    current = await _active_for(session, scope_, pid)
    if current is not None and current.reason == reason_:
        return current, False
    if current is not None:
        _close(current, by)
        await session.flush()
    halt = TradingHalt(scope=scope_, portfolio_id=pid, active=True, reason=reason_, set_by=by)
    session.add(halt)
    await session.flush()
    await session.refresh(halt, ["set_at"])
    _event(session, "halt.set", halt, by, agent_id, reason_)
    return halt, True


async def clear_halt(
    session: AsyncSession, *, scope: Any, portfolio_id: uuid.UUID | None, by: str,
    agent_id: uuid.UUID | None = None, reason: Any = None,
) -> TradingHalt | None:
    """Clear the active halt on that target; None when there was none."""
    note = clean_reason(reason, required=False)
    scope_, pid = await _target(session, scope, portfolio_id)
    await _lock(session)
    halt = await _active_for(session, scope_, pid)
    if halt is None:
        return None
    return await _clear(session, halt, by, agent_id, note)


async def clear_halt_by_id(
    session: AsyncSession, halt_id: int, *, by: str
) -> tuple[TradingHalt, bool]:
    """Admin «Revoca»: (the halt, whether it was cleared now); NOT_FOUND for an unknown id,
    an already cleared halt is returned as it is."""
    await _lock(session)
    halt = await session.get(TradingHalt, halt_id, populate_existing=True)
    if halt is None:
        raise SMTError(ErrorCode.NOT_FOUND, "halt not found", {"halt_id": halt_id})
    if not halt.active:
        return halt, False
    return await _clear(session, halt, by, None, None), True


async def _clear(session: AsyncSession, halt: TradingHalt, by: str,
                 agent_id: uuid.UUID | None, note: str | None) -> TradingHalt:
    _close(halt, by)
    await session.flush()
    _event(session, "halt.cleared", halt, by, agent_id, note)
    return halt


def _covers(portfolio_ids: list[uuid.UUID] | None):
    if portfolio_ids is None:
        return TradingHalt.active
    return TradingHalt.active & or_(
        TradingHalt.scope == HaltScope.global_.value,
        TradingHalt.portfolio_id.in_(portfolio_ids),
    )


async def covering_halt(session: AsyncSession, portfolio_id: uuid.UUID) -> TradingHalt | None:
    """The active halt covering the portfolio (the most recent one), or None."""
    return await session.scalar(
        select(TradingHalt).where(_covers([portfolio_id]))
        .order_by(TradingHalt.set_at.desc(), TradingHalt.id.desc()).limit(1)
    )


async def active_halts(
    session: AsyncSession, portfolio_ids: list[uuid.UUID] | None = None
) -> list[TradingHalt]:
    """Active halts, newest first: all of them, or those covering the given portfolios."""
    return list(await session.scalars(
        select(TradingHalt).where(_covers(portfolio_ids))
        .order_by(TradingHalt.set_at.desc(), TradingHalt.id.desc())
    ))


async def halt_history(session: AsyncSession, limit: int = 50) -> list[TradingHalt]:
    """Cleared halts, the most recently cleared first."""
    return list(await session.scalars(
        select(TradingHalt).where(TradingHalt.active.is_(False))
        .order_by(TradingHalt.cleared_at.desc(), TradingHalt.id.desc()).limit(limit)
    ))


async def _author_names(session: AsyncSession, halts: list[TradingHalt]) -> dict[str, str]:
    from smtrading.agents.models import Agent

    ids = set()
    for h in halts:
        for who in (h.set_by, h.cleared_by):
            if who and who.startswith("agent:"):
                try:
                    ids.add(uuid.UUID(who.removeprefix("agent:")))
                except ValueError:
                    pass
    if not ids:
        return {}
    rows = await session.execute(select(Agent.id, Agent.name).where(Agent.id.in_(ids)))
    return {f"agent:{i}": name for i, name in rows.all()}


def _who(value: str | None, names: dict[str, str]) -> str | None:
    if value is None:
        return None
    if value in names:
        return names[value]
    return value.split(":", 1)[1] if ":" in value else value


async def halt_dtos(session: AsyncSession, halts: list[TradingHalt]) -> list[dict[str, Any]]:
    """{id, scope, portfolio_id, portfolio, active, reason, set_by, set_by_name, set_at,
    cleared_by, cleared_by_name, cleared_at}; names: the agent's name or the admin email."""
    names = await _author_names(session, halts)
    pids = {h.portfolio_id for h in halts if h.portfolio_id is not None}
    portfolios = {}
    if pids:
        rows = await session.execute(
            select(Portfolio.id, Portfolio.name).where(Portfolio.id.in_(pids))
        )
        portfolios = dict(rows.all())
    return [{
        "id": h.id, "scope": h.scope, "portfolio_id": h.portfolio_id,
        "portfolio": portfolios.get(h.portfolio_id), "active": h.active, "reason": h.reason,
        "set_by": h.set_by, "set_by_name": _who(h.set_by, names), "set_at": h.set_at,
        "cleared_by": h.cleared_by, "cleared_by_name": _who(h.cleared_by, names),
        "cleared_at": h.cleared_at,
    } for h in halts]
