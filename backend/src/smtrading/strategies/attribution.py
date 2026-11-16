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

Two sources, one set of rules: `strategy_of_order` (one order: a few targeted queries; the
mention scan of rule 4 runs only when rules 1-3 give nothing) and `order_attributor` (the
«Strategie» page: the decisions of the orders' portfolios loaded once, then every order
attributed in memory).
"""

import json
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision
from smtrading.engine.models import Order

STRATEGY_REF = re.compile(r"\bSTR-\d+\b")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KIND_ORDER = {"proposal": 0, "approval": 1}
_MAX_PARENTS = 5


def _ref(value: Any) -> str | None:
    if isinstance(value, str):
        m = STRATEGY_REF.fullmatch(value.strip())
        return m.group(0) if m else None
    return None


def _details(d: Decision) -> dict:
    return d.details if isinstance(d.details, dict) else {}


class _Source(Protocol):
    async def by_ref(self, order: Order, ref: str) -> list[Decision]: ...  # oldest first
    async def mentions(self, order: Order, needles: list[str]) -> list[Decision]: ...  # newest
    async def decision(self, decision_id: int) -> Decision | None: ...
    async def order(self, order_id: uuid.UUID) -> Order | None: ...


class _Sql:
    """One order: targeted queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def _scope(order: Order):
        return or_(Decision.portfolio_id == order.portfolio_id, Decision.portfolio_id.is_(None))

    async def by_ref(self, order: Order, ref: str) -> list[Decision]:
        return list(await self._s.scalars(
            select(Decision)
            .where(Decision.details["ref"].astext == ref, self._scope(order))
            .order_by(Decision.id)))

    async def mentions(self, order: Order, needles: list[str]) -> list[Decision]:
        text = cast(Decision.details, Text)
        return list(await self._s.scalars(
            select(Decision)
            .where(or_(*(text.contains(n, autoescape=True) for n in needles)),
                   self._scope(order))
            .order_by(Decision.id.desc())))

    async def decision(self, decision_id: int) -> Decision | None:
        return await self._s.get(Decision, decision_id)

    async def order(self, order_id: uuid.UUID) -> Order | None:
        return await self._s.get(Order, order_id)


class _Memory:
    """Many orders: decisions and orders loaded once, indexed by `details.ref` and by
    the ids named in the details."""

    def __init__(self, decisions: Iterable[Decision], orders: dict[uuid.UUID, Order]) -> None:
        self._orders = orders
        self._by_id: dict[int, Decision] = {}
        self._by_ref: dict[str, list[Decision]] = defaultdict(list)
        self._by_uuid: dict[str, list[Decision]] = defaultdict(list)
        for d in sorted(decisions, key=lambda d: d.id):
            self._by_id[d.id] = d
            ref = _details(d).get("ref")
            if isinstance(ref, str):
                self._by_ref[ref].append(d)
            for u in set(_UUID.findall(json.dumps(d.details, default=str))):
                self._by_uuid[u].append(d)

    @staticmethod
    def _mine(order: Order, rows: Iterable[Decision]) -> list[Decision]:
        return [d for d in rows if d.portfolio_id in (order.portfolio_id, None)]

    async def by_ref(self, order: Order, ref: str) -> list[Decision]:
        return self._mine(order, self._by_ref.get(ref, []))

    async def mentions(self, order: Order, needles: list[str]) -> list[Decision]:
        rows = {d.id: d for n in needles for d in self._by_uuid.get(n, [])}
        return self._mine(order, sorted(rows.values(), key=lambda d: d.id, reverse=True))

    async def decision(self, decision_id: int) -> Decision | None:
        return self._by_id.get(decision_id)

    async def order(self, order_id: uuid.UUID) -> Order | None:
        return self._orders.get(order_id)


async def _strategy_in(src: _Source, d: Decision) -> str | None:
    details = _details(d)
    found = _ref(details.get("strategy_id")) or _ref(details.get("ref"))
    if found:
        return found
    linked = details.get("strategy_decision_id")
    if isinstance(linked, int) and not isinstance(linked, bool):
        target = await src.decision(linked)
        if target is not None:
            td = _details(target)
            found = _ref(td.get("strategy_id")) or _ref(td.get("ref"))
            if found:
                return found
    m = STRATEGY_REF.search(d.subject or "")
    return m.group(0) if m else None


async def _by_proposal(src: _Source, order: Order, proposal: str) -> str | None:
    rows = await src.by_ref(order, proposal)
    rows.sort(key=lambda d: _KIND_ORDER.get(d.kind, 2))
    for d in rows:
        if found := await _strategy_in(src, d):
            return found
    return None


async def _by_mention(src: _Source, order: Order) -> str | None:
    needles = [str(order.id)] + ([str(order.oco_group_id)] if order.oco_group_id else [])
    for d in await src.mentions(order, needles):
        ref = _details(d).get("ref")
        if (isinstance(ref, str) and not _ref(ref)
                and (found := await _by_proposal(src, order, ref))):
            return found
        if found := await _strategy_in(src, d):
            return found
    return None


async def _resolve(src: _Source, order: Order) -> str | None:
    current: Order | None = order
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen and len(seen) < _MAX_PARENTS:
        seen.add(current.id)
        coid = (current.client_order_id or "").strip()
        if coid:
            if found := _ref(coid):
                return found
            if found := await _by_proposal(src, current, coid):
                return found
        if current.parent_id is None:
            break
        current = await src.order(current.parent_id)
    return await _by_mention(src, order)


async def strategy_of_order(session: AsyncSession, order: Order) -> str | None:
    """The strategy ref (`STR-103`) of `order`, or None when unknown (see the module)."""
    return await _resolve(_Sql(session), order)


async def order_attributor(
    session: AsyncSession, orders: Iterable[Order],
) -> Callable[[Order], Awaitable[str | None]]:
    """Loads, once, what attributing `orders` needs (their parents, the decisions of their
    portfolios or without one, the decisions those point to) and returns the attribution
    of one of them, made in memory with the rules of `strategy_of_order`."""
    known: dict[uuid.UUID, Order] = {o.id: o for o in orders}
    pids = {o.portfolio_id for o in known.values()}
    for _ in range(_MAX_PARENTS):
        missing = {o.parent_id for o in known.values()
                   if o.parent_id is not None and o.parent_id not in known}
        if not missing:
            break
        for o in await session.scalars(select(Order).where(Order.id.in_(missing))):
            known[o.id] = o
    decisions = list(await session.scalars(
        select(Decision).where(or_(Decision.portfolio_id.in_(pids),
                                   Decision.portfolio_id.is_(None))))) if pids else []
    have = {d.id for d in decisions}
    linked = {v for d in decisions
              if isinstance(v := _details(d).get("strategy_decision_id"), int)
              and not isinstance(v, bool) and v not in have}
    if linked:
        decisions += list(await session.scalars(select(Decision).where(Decision.id.in_(linked))))
    src = _Memory(decisions, known)

    async def attribute(order: Order) -> str | None:
        return await _resolve(src, order)

    return attribute
