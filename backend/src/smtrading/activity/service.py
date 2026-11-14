"""Activity feed and decisions log.

`record_activity` runs inside the transaction of the fact it records (called by
`events.queue_event` / `events.notify`): a rollback drops it, a commit keeps it even
with Redis or the worker down. Decisions are append-only.

Visibility for an agent (feed and decisions): rows of its (non archived) portfolios,
or global rows (no portfolio) that are system-wide, its own, or decisions.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.mapping import MAX_SUMMARY, ActivityDraft
from smtrading.activity.models import (
    Activity,
    ActivityLevel,
    Decision,
    DecisionKind,
    TelegramState,
)
from smtrading.agents.models import Agent
from smtrading.agents.service import agent_portfolio_ids, require_permission
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio

ACTIVITY_KEYS = "smt_activity_keys"
FEED_SETTLE_S = 3
MAX_SYMBOLS = 50
MAX_SYMBOL_LEN = 32
MAX_DETAILS_BYTES = 8192
_SENT = (TelegramState.queued.value, TelegramState.digest.value)


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def record_activity(
    session: AsyncSession,
    draft: ActivityDraft,
    *,
    portfolio_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None,
) -> None:
    """Add the activity row to the session (no flush). A fact about an order announced
    twice in the same transaction (e.g. a rejection by `notify` and by `queue_event`)
    is recorded once: the first one wins. Fills are told apart by their fill_id."""
    from smtrading.events import json_safe  # events imports this module

    order_id = draft.data.get("order_id")
    if order_id is not None:
        keys: dict = session.info.setdefault(ACTIVITY_KEYS, {})
        key = (draft.type, str(order_id), str(draft.data.get("fill_id")))
        seen = keys.get(key)
        if seen is not None and seen in session:  # not rolled back meanwhile
            return
    row = Activity(
        type=draft.type, level=str(draft.level), agent_id=_uuid(agent_id),
        portfolio_id=_uuid(portfolio_id), summary=draft.summary[:MAX_SUMMARY],
        data=json_safe(draft.data),
        telegram=TelegramState.pending.value if draft.direct else None,
    )
    session.add(row)
    if order_id is not None:
        keys[key] = row


def clear_activity_keys(session: AsyncSession) -> None:
    session.info.pop(ACTIVITY_KEYS, None)


def _invalid(field: str, message: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"field": field})


def _clean_symbols(symbols: Any) -> list[str]:
    if symbols is None:
        return []
    if not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS:
        raise _invalid("symbols", f"symbols must be a list of at most {MAX_SYMBOLS} symbols")
    out: list[str] = []
    for s in symbols:
        if not isinstance(s, str) or not s.strip() or len(s.strip()) > MAX_SYMBOL_LEN:
            raise _invalid("symbols", f"each symbol must be 1-{MAX_SYMBOL_LEN} characters")
        sym = s.strip().upper()
        if sym not in out:
            out.append(sym)
    return out


async def log_decision(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    kind: str,
    summary: str,
    portfolio_id: uuid.UUID | None = None,
    symbols: list[str] | None = None,
    details: dict | None = None,
) -> Decision:
    """Store an agent's decision (append-only) and its activity row."""
    from smtrading.events import json_safe

    if kind not in {k.value for k in DecisionKind}:
        raise _invalid("kind", "kind must be one of " + ", ".join(k.value for k in DecisionKind))
    text = summary.strip() if isinstance(summary, str) else ""
    if not text or len(text) > MAX_SUMMARY:
        raise _invalid("summary", f"summary must be 1-{MAX_SUMMARY} characters")
    clean_symbols = _clean_symbols(symbols)
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise _invalid("details", "details must be an object")
    details = json_safe(details)
    if len(json.dumps(details, ensure_ascii=False).encode()) > MAX_DETAILS_BYTES:
        raise _invalid("details", f"details must be at most {MAX_DETAILS_BYTES} bytes of JSON")
    if portfolio_id is not None:
        await require_permission(session, agent_id, portfolio_id, "read")

    decision = Decision(
        agent_id=agent_id, portfolio_id=portfolio_id, kind=kind, summary=text,
        details=details, symbols=clean_symbols,
    )
    session.add(decision)
    await session.flush()
    record_activity(
        session,
        ActivityDraft(f"decision.{kind}", ActivityLevel.info, False, text, {
            "decision_id": decision.id, "symbols": clean_symbols, "details": details,
        }),
        portfolio_id=portfolio_id, agent_id=agent_id,
    )
    await session.flush()
    return decision


async def _visible(session: AsyncSession, model, viewer_agent_id: uuid.UUID):
    pids = await agent_portfolio_ids(session, viewer_agent_id)
    if model is Decision:  # global decisions are visible to every agent
        global_ok = model.portfolio_id.is_(None)
    else:
        global_ok = and_(model.portfolio_id.is_(None), or_(
            model.agent_id.is_(None), model.agent_id == viewer_agent_id,
            model.type.like("decision.%"),
        ))
    return or_(model.portfolio_id.in_(pids), global_ok)


async def list_decisions(
    session: AsyncSession,
    *,
    viewer_agent_id: uuid.UUID | None,
    portfolio_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[Decision]:
    """Newest first. `viewer_agent_id=None` is the admin: everything is visible."""
    stmt = select(Decision)
    if viewer_agent_id is not None:
        stmt = stmt.where(await _visible(session, Decision, viewer_agent_id))
    if portfolio_id is not None:
        stmt = stmt.where(Decision.portfolio_id == portfolio_id)
    if agent_id is not None:
        stmt = stmt.where(Decision.agent_id == agent_id)
    if kind is not None:
        stmt = stmt.where(Decision.kind == kind)
    if since is not None:
        stmt = stmt.where(Decision.created_at > since)
    if before_id is not None:
        stmt = stmt.where(Decision.id < before_id)
    return list(await session.scalars(stmt.order_by(Decision.id.desc()).limit(limit)))


async def list_feed(
    session: AsyncSession,
    viewer_agent_id: uuid.UUID,
    *,
    after_id: int | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> tuple[list[dict], int | None]:
    """(items, next_after_id), the get_triggered_alerts contract: without a cursor or
    `since`, the newest `limit` rows newest first and next_after_id = the newest id;
    with either, ascending ids, only rows older than FEED_SETTLE_S (a slower
    transaction may still commit a lower id), next_after_id = the last id returned or
    the cursor itself. Items carry Python values (datetime, UUID): json_safe them."""
    if after_id is not None and await session.scalar(
        select(Activity.id).where(Activity.id == after_id)
    ) is None:
        raise _invalid("after_id", "unknown after_id")
    stmt = (
        select(Activity, Agent.name, Portfolio.name)
        .outerjoin(Agent, Agent.id == Activity.agent_id)
        .outerjoin(Portfolio, Portfolio.id == Activity.portfolio_id)
        .where(await _visible(session, Activity, viewer_agent_id))
    )
    if after_id is None and since is None:
        rows = (await session.execute(stmt.order_by(Activity.id.desc()).limit(limit))).all()
        next_after = rows[0][0].id if rows else None
    else:
        stmt = stmt.where(Activity.ts <= func.now() - timedelta(seconds=FEED_SETTLE_S))
        if after_id is not None:
            stmt = stmt.where(Activity.id > after_id)
        if since is not None:
            stmt = stmt.where(Activity.ts > since)
        rows = (await session.execute(stmt.order_by(Activity.id).limit(limit))).all()
        next_after = rows[-1][0].id if rows else after_id
    items = [
        {
            "id": a.id, "ts": a.ts, "type": a.type, "level": a.level,
            "agent": agent_name, "agent_id": a.agent_id,
            "portfolio": portfolio_name, "portfolio_id": a.portfolio_id,
            "summary": a.summary, "data": a.data, "already_sent": a.telegram in _SENT,
        }
        for a, agent_name, portfolio_name in rows
    ]
    return items, next_after
