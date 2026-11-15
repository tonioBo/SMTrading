"""Activity feed and decisions log.

`record_activity` runs inside the transaction of the fact it records (called by
`events.queue_event` / `events.notify`): a rollback drops it, a commit keeps it even
with Redis or the worker down. Decisions are append-only.

Visibility for an agent (feed and decisions): rows of its (non archived) portfolios,
or global rows (no portfolio) that are system-wide, its own, or decisions.

Feed cursor and the settle window: ids are assigned at insert, rows become visible at
commit, so a row can appear with an id lower than one already returned. The feed only
moves its cursor over rows older than FEED_SETTLE_S; a transaction that stays open
longer than that between recording an activity and committing can still slip behind
the cursor (business transactions are short; a transaction-id based cursor would close
the gap). Items newer than the cursor may be returned twice: consumers dedupe by id.
"""

import json
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, event, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from smtrading.activity.mapping import MAX_SUMMARY, ActivityDraft
from smtrading.activity.models import (
    Activity,
    ActivityLevel,
    Decision,
    DecisionKind,
    DecisionOutcome,
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
MAX_REASON = 300
MAX_SUBJECT = 200
MAX_TYPE = 64
MAX_LEVEL = 16
# direct events (telegram not NULL) are SMTrading's job: sent, queued, in the night digest or
# still to plan. Only the ones it will never send (skipped, failed) may be repeated.
_NOT_SENT = (TelegramState.skipped.value, TelegramState.failed.value)


def _already_sent(telegram: str | None) -> bool:
    return telegram is not None and telegram not in _NOT_SENT


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _text(value: Any, limit: int) -> str:
    text = "" if value is None else str(getattr(value, "value", value))
    return text.replace("\x00", "")[:limit]


def jsonb_safe(obj: Any) -> Any:
    """JSON-safe for a jsonb column whatever comes in: json_safe's conversions, then
    no NUL characters (Postgres refuses them), non-finite floats and unknown objects
    as strings. Used for activity data and notification payloads; json_safe itself is
    unchanged."""
    from smtrading.events import json_safe  # events imports this module

    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if obj is None or isinstance(obj, (bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, dict):
        return {jsonb_safe(str(k)): jsonb_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonb_safe(v) for v in obj]
    safe = json_safe(obj)
    return jsonb_safe(str(obj)) if safe is obj else jsonb_safe(safe)


def record_activity(
    session: AsyncSession,
    draft: ActivityDraft,
    *,
    portfolio_id: uuid.UUID | str | None,
    agent_id: uuid.UUID | str | None,
) -> None:
    """Add the activity row to the session (no flush). A fact about an order announced
    twice in the same transaction (e.g. a rejection by `notify` and by `queue_event`)
    is recorded once: the first one wins. Fills are told apart by their fill_id.

    It never aborts the business transaction: every field is normalised to fit its
    column (no foreign key on agent_id, no NUL characters), the data made JSON safe
    (Decimal, UUID, datetime, enums and sets of kill-switch, fill or reset payloads;
    anything else as its str())."""
    data = draft.data if isinstance(draft.data, dict) else {"value": draft.data}
    order_id = data.get("order_id")
    if order_id is not None:
        keys: dict = session.info.setdefault(ACTIVITY_KEYS, {})
        key = (str(draft.type), str(order_id), str(data.get("fill_id")))
        seen = keys.get(key)
        if seen is not None and seen in session:  # not rolled back meanwhile
            return
    row = Activity(
        type=_text(draft.type, MAX_TYPE), level=_text(draft.level, MAX_LEVEL),
        agent_id=_uuid(agent_id), portfolio_id=_uuid(portfolio_id),
        summary=_text(draft.summary, MAX_SUMMARY), data=jsonb_safe(data),
        telegram=TelegramState.pending.value if draft.direct else None,
    )
    session.add(row)
    if order_id is not None:
        keys[key] = row


def clear_activity_keys(session: AsyncSession | Session) -> None:
    session.info.pop(ACTIVITY_KEYS, None)


@event.listens_for(Session, "after_commit")
def _keys_after_commit(session: Session) -> None:
    if not session.in_nested_transaction():  # a savepoint release is not the commit
        clear_activity_keys(session)


@event.listens_for(Session, "after_soft_rollback")
def _keys_after_rollback(session: Session, previous_transaction) -> None:
    if not session.in_nested_transaction():
        clear_activity_keys(session)


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


def _clean_text(value: Any, field: str, limit: int) -> str | None:
    """An optional text: stripped, empty -> None, at most `limit` characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(field, f"{field} must be a string")
    text = value.strip()
    if len(text) > limit:
        raise _invalid(field, f"{field} must be at most {limit} characters")
    return text or None


async def log_decision(
    session: AsyncSession,
    agent_id: uuid.UUID,
    *,
    kind: str,
    summary: str,
    portfolio_id: uuid.UUID | None = None,
    symbols: list[str] | None = None,
    details: dict | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    subject: str | None = None,
) -> Decision:
    """Store an agent's decision (append-only) and its activity row. outcome, reason and
    subject are optional: in the activity data only when set."""
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
    outcomes = [o.value for o in DecisionOutcome]
    outcome_ = outcome.strip() or None if isinstance(outcome, str) else outcome
    if outcome_ is not None and outcome_ not in outcomes:
        raise _invalid("outcome", "outcome must be one of " + ", ".join(outcomes))
    structured = {
        "outcome": outcome_,
        "reason": _clean_text(reason, "reason", MAX_REASON),
        "subject": _clean_text(subject, "subject", MAX_SUBJECT),
    }
    if portfolio_id is not None:
        await require_permission(session, agent_id, portfolio_id, "read")

    decision = Decision(
        agent_id=agent_id, portfolio_id=portfolio_id, kind=kind, summary=text,
        details=details, symbols=clean_symbols, **structured,
    )
    session.add(decision)
    await session.flush()
    record_activity(
        session,
        ActivityDraft(f"decision.{kind}", ActivityLevel.info, False, text, {
            "decision_id": decision.id, "symbols": clean_symbols, "details": details,
            **{k: v for k, v in structured.items() if v is not None},
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
        # global live.* alerts (e.g. a link armed before any portfolio is linked) name the
        # link and are for the admins and Telegram only, never for an agent
        global_ok = and_(model.portfolio_id.is_(None), ~model.type.like("live.%"), or_(
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
    `since`, the newest `limit` rows newest first (fresh ones included) and
    next_after_id = the newest id older than FEED_SETTLE_S (None if there is none), so
    rows still settling come again on the next call: consumers dedupe by id. With a
    cursor or `since`, ascending ids, only rows older than FEED_SETTLE_S,
    next_after_id = the last id returned or the cursor itself. Any after_id up to the
    newest id is accepted (a cursor stays valid when its row's portfolio is archived or
    unassigned, and nothing tells whether a hidden row exists); a larger one is unknown. Items carry Python values (datetime, UUID):
    json_safe them."""
    visible = await _visible(session, Activity, viewer_agent_id)
    settled = Activity.ts <= func.now() - timedelta(seconds=FEED_SETTLE_S)
    if after_id is not None and (after_id < 0 or after_id > (
        await session.scalar(select(func.max(Activity.id))) or 0
    )):  # any id up to the newest is a valid cursor (its row may be hidden since)
        raise _invalid("after_id", "unknown after_id")
    stmt = (
        select(Activity, Agent.name, Portfolio.name)
        .outerjoin(Agent, Agent.id == Activity.agent_id)
        .outerjoin(Portfolio, Portfolio.id == Activity.portfolio_id)
        .where(visible)
    )
    if after_id is None and since is None:
        rows = (await session.execute(stmt.order_by(Activity.id.desc()).limit(limit))).all()
        next_after = await session.scalar(select(func.max(Activity.id)).where(visible, settled))
    else:
        stmt = stmt.where(settled)
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
            "summary": a.summary, "data": a.data, "already_sent": _already_sent(a.telegram),
        }
        for a, agent_name, portfolio_name in rows
    ]
    return items, next_after
