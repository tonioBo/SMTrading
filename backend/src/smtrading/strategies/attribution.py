"""Which strategy (STR-xxx) an order belongs to, read from the agents' decisions.

One implementation for the Telegram fill notification and the per-strategy results.
The shapes are the ones the agents write (production, 16/11/2026):

- the PM proposal (`kind=proposal`) has `details.ref` = the proposal id, which is the
  order's `client_order_id` (`PROP-AGG-20261116-2130-1`), and `details.strategy_id`;
- the RM approval (`kind=approval`) has the same `details.ref` and
  `details.strategy_decision_id` (the id of the CEO's `strategy` decision, whose
  `details.ref`/`details.strategy_id` is the STR ref), and names the ref in its subject;
- bracket legs have `parent_id` = the entry order (no client_order_id);
- OCO legs placed later (e.g. replacing day legs with gtc ones) have no client_order_id
  nor parent: a decision of the same portfolio names their id (or their `oco_group_id`)
  in its details, with `details.ref` = the proposal id.

Rules, in order (decisions of the order's portfolio, or without a portfolio, only):
1. `client_order_id` itself a strategy ref (`STR-107`) → that ref;
2. decisions whose `details.ref` equals the `client_order_id`, proposals first, then
   approvals, then the others (oldest first within a kind): the first that yields a ref
   (`details.strategy_id`, then `details.ref` when it is a STR ref, then the decision
   `details.strategy_decision_id` points to, then the first `STR-n` in the subject);
3. no answer and the order has a `parent_id` → the parent, by the same rules;
4. still none → the newest decision naming the order's id or its `oco_group_id` in its
   details: its `details.ref` by rule 2 when it is a proposal id, else its own ref.
Unknown → None (the callers show «Senza strategia»).
"""

import re
import uuid
from typing import Any

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision
from smtrading.engine.models import Order

STRATEGY_REF = re.compile(r"\bSTR-\d+\b")
_KIND_ORDER = {"proposal": 0, "approval": 1}
_MAX_PARENTS = 5


def _ref(value: Any) -> str | None:
    if isinstance(value, str):
        m = STRATEGY_REF.fullmatch(value.strip())
        return m.group(0) if m else None
    return None


def _portfolio_scope(order: Order):
    return or_(Decision.portfolio_id == order.portfolio_id, Decision.portfolio_id.is_(None))


async def _strategy_in(session: AsyncSession, d: Decision) -> str | None:
    details = d.details if isinstance(d.details, dict) else {}
    found = _ref(details.get("strategy_id")) or _ref(details.get("ref"))
    if found:
        return found
    linked = details.get("strategy_decision_id")
    if isinstance(linked, int) and not isinstance(linked, bool):
        target = await session.get(Decision, linked)
        if target is not None:
            td = target.details if isinstance(target.details, dict) else {}
            found = _ref(td.get("strategy_id")) or _ref(td.get("ref"))
            if found:
                return found
    m = STRATEGY_REF.search(d.subject or "")
    return m.group(0) if m else None


async def _by_proposal(session: AsyncSession, order: Order, proposal: str) -> str | None:
    rows = list(await session.scalars(
        select(Decision)
        .where(Decision.details["ref"].astext == proposal, _portfolio_scope(order))
        .order_by(Decision.id)
    ))
    rows.sort(key=lambda d: _KIND_ORDER.get(d.kind, 2))
    for d in rows:
        if found := await _strategy_in(session, d):
            return found
    return None


async def _by_mention(session: AsyncSession, order: Order) -> str | None:
    needles = [str(order.id)] + ([str(order.oco_group_id)] if order.oco_group_id else [])
    text = cast(Decision.details, Text)
    rows = await session.scalars(
        select(Decision)
        .where(or_(*(text.contains(n, autoescape=True) for n in needles)),
               _portfolio_scope(order))
        .order_by(Decision.id.desc())
    )
    for d in rows:
        ref = (d.details or {}).get("ref") if isinstance(d.details, dict) else None
        if (isinstance(ref, str) and not _ref(ref)
                and (found := await _by_proposal(session, order, ref))):
            return found
        if found := await _strategy_in(session, d):
            return found
    return None


async def strategy_of_order(session: AsyncSession, order: Order) -> str | None:
    """The strategy ref (`STR-103`) of `order`, or None when unknown (see the module)."""
    current: Order | None = order
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen and len(seen) < _MAX_PARENTS:
        seen.add(current.id)
        coid = (current.client_order_id or "").strip()
        if coid:
            if found := _ref(coid):
                return found
            if found := await _by_proposal(session, current, coid):
                return found
        if current.parent_id is None:
            break
        current = await session.get(Order, current.parent_id)
    return await _by_mention(session, order)
