"""«Risultati agenti»: what each agent did and cost in a period, the results of each
strategy and the team summary. Read-only, for the portal (never an MCP tool).

Periods (local days, `settings.tz`): `week` = from Monday of the current week, `1m` = the
last 30 days, `3m` = the last 90 days (today included), `all` = everything.

Per agent (every SMTrading agent, by name, case insensitive):
- `decisions`: its decisions created in the period, by kind (every kind, 0 included);
- proposals: its `proposal` decisions of the period; each one's outcome is the latest
  `approval` or `veto` of the same portfolio with the same `details.ref` (`approved`,
  `vetoed`, else `pending`); `approval_rate_pct` = approved / (approved + vetoed);
- `lessons_written`: its `lesson` decisions; `lessons_about`: the period's lessons whose
  `details.agents` names it (its name, its name without «SMTrading», or its initials:
  «pm» for «Portfolio Manager»; «tutti» names nobody in particular); `lessons_repeated`:
  those of them with a non-empty `details.repeats`;
- `mcp_calls` / `mcp_errors`: its `mcp_audit_log` rows (outcome other than `ok`);
- `orders_executed`: its orders (`orders.agent_id`) with at least one fill in the period
  (non-archived portfolios);
- cost: `agent_cost_days` of the Paperclip agents matched to it (agent map of the
  Paperclip settings, else same name), USD: `cost_usd` = API + subscription + other (the
  value of its runs), `billed_usd` = API + other (what counts against the budget);
  `cost_per_order_usd` = cost_usd / orders_executed (None without orders).

Per strategy (STR ref, «Senza strategia» = None; trades of `performance.strategies`: FIFO
lots over the whole history, attributed by the opening order): `trades_opened` (opened in
the period), `trades_closed` (fully closed in the period), `win_rate_pct` (closed trades
whose realised P/L minus costs is > 0), `realized_pnl_eur` (gross) / `costs_eur` (fees +
FX fees) / `net_pnl_eur` of the trades closed in the period, `avg_holding_s` (closed in
the period), `open_positions` (now: portfolio+symbol pairs with an open lot). EUR at the
current FX rate; None («—») when a rate is missing.

Team: `pnl_eur` = Σ strategies' net P/L, `orders_executed`, proposals / approvals /
vetoes / lessons of the period, `rules_active` (now: latest `team_rule` per ref with
`status` active), cost of every Paperclip agent (unmatched ones too), `pnl_per_usd` =
pnl_eur / cost_usd, `cost_per_order_usd`.
"""

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.activity.models import Decision, DecisionKind
from smtrading.agents.models import Agent, McpAuditLog
from smtrading.common.config import get_settings
from smtrading.engine.quotesource import QuoteSource
from smtrading.paperclip.config import load_config, load_state, match_agent, smt_agents
from smtrading.paperclip.models import AgentCostDay
from smtrading.performance.strategies import _fills, _money, _Rates, _sum, fifo_trades
from smtrading.signals.service import strategy_states
from smtrading.strategies.attribution import order_attributor

PERIODS = ("week", "1m", "3m", "all")
_DAYS_BACK = {"1m": 29, "3m": 89}
_PCT = Decimal("0.1")
_USD = Decimal("0.01")
_EVERYONE = {"tutti", "all", "team", "tutto il team"}


def period_bounds(period: str, now: datetime, tz: str) -> tuple[date | None, datetime | None]:
    """(first local day, its start as an aware datetime); (None, None) for `all`."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    if period == "all":
        return None, None
    first = today - timedelta(days=today.weekday() if period == "week" else _DAYS_BACK[period])
    return first, datetime.combine(first, time(0), tzinfo=zone)


def agent_aliases(name: str) -> set[str]:
    """How a lesson's `details.agents` may name an agent: «Portfolio Manager» →
    {portfolio manager, pm}; «CEO SMTrading» → {ceo smtrading, ceo}."""
    full = " ".join(name.casefold().split())
    words = [w for w in re.split(r"[\s_\-·]+", full) if w and w != "smtrading"]
    out = {full}
    if words:
        out.add(" ".join(words))
        if len(words) > 1:
            out.add("".join(w[0] for w in words))
    return out


def _pct(part: int, whole: int) -> Decimal | None:
    return (Decimal(part) * 100 / whole).quantize(_PCT, ROUND_HALF_UP) if whole else None


def _ratio(num: Decimal | None, den: Decimal | int | None, q: Decimal) -> Decimal | None:
    if num is None or not den:
        return None
    return (Decimal(num) / Decimal(den)).quantize(q, ROUND_HALF_UP)


def _details(d: Decision) -> dict:
    return d.details if isinstance(d.details, dict) else {}


@dataclass
class AgentResult:
    agent_id: uuid.UUID
    name: str
    active: bool
    decisions: dict[str, int]
    decisions_total: int = 0
    proposals: int = 0
    approved: int = 0
    vetoed: int = 0
    pending: int = 0
    approval_rate_pct: Decimal | None = None
    lessons_written: int = 0
    lessons_about: int = 0
    lessons_repeated: int = 0
    mcp_calls: int = 0
    mcp_errors: int = 0
    mcp_error_pct: Decimal | None = None
    orders_executed: int = 0
    paperclip_names: list[str] = field(default_factory=list)
    runs: int = 0
    cost_usd: Decimal = Decimal(0)
    billed_usd: Decimal = Decimal(0)
    cost_per_order_usd: Decimal | None = None


@dataclass
class StrategyResult:
    ref: str | None
    name: str | None
    status: str | None
    portfolios: list[str]
    trades_opened: int
    trades_closed: int
    wins: int
    win_rate_pct: Decimal | None
    realized_pnl_eur: Decimal | None
    costs_eur: Decimal | None
    net_pnl_eur: Decimal | None
    avg_holding_s: int | None
    open_positions: int


@dataclass
class TeamResult:
    pnl_eur: Decimal | None
    costs_eur: Decimal | None
    orders_executed: int
    proposals: int
    approvals: int
    vetoes: int
    approval_rate_pct: Decimal | None
    lessons: int
    lessons_repeated: int
    rules_active: int
    cost_usd: Decimal
    billed_usd: Decimal
    unmatched_cost_usd: Decimal
    pnl_per_usd: Decimal | None
    cost_per_order_usd: Decimal | None


@dataclass
class AgentResults:
    period: str
    from_: date | None
    to: date
    last_read_at: Any
    team: TeamResult
    agents: list[AgentResult]
    strategies: list[StrategyResult]


async def _rules_active(session: AsyncSession) -> int:
    latest: dict[str, str | None] = {}
    for d in await session.scalars(
            select(Decision).where(Decision.kind == DecisionKind.team_rule)
            .order_by(Decision.id)):
        det = _details(d)
        ref = det.get("ref") if isinstance(det.get("ref"), str) else f"#{d.id}"
        latest[ref] = det.get("status")
    return sum(1 for s in latest.values() if s == "active")


def _outcomes(decisions: list[Decision]) -> dict[int, str]:
    """proposal decision id → approved | vetoed | pending (latest approval/veto by ref)."""
    verdict: dict[tuple, str] = {}
    for d in sorted(decisions, key=lambda d: d.id):
        ref = _details(d).get("ref")
        if d.kind in ("approval", "veto") and isinstance(ref, str):
            verdict[(d.portfolio_id, ref)] = "approved" if d.kind == "approval" else "vetoed"
    out = {}
    for d in decisions:
        ref = _details(d).get("ref")
        if d.kind == "proposal":
            out[d.id] = verdict.get((d.portfolio_id, ref), "pending") \
                if isinstance(ref, str) else "pending"
    return out


def _named(lesson: Decision) -> set[str]:
    raw = _details(lesson).get("agents")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {" ".join(v.casefold().split()) for v in raw if isinstance(v, str)} - _EVERYONE


async def _strategies(session: AsyncSession, qs: QuoteSource, since: datetime | None
                      ) -> tuple[list[StrategyResult], dict[uuid.UUID, str | None]]:
    """The strategies' rows and the agent of each order executed in the period."""
    fills, portfolios, orders = await _fills(session)
    executed = {f.order_id: orders[f.order_id].agent_id for f in fills
                if since is None or f.ts >= since}
    trades = fifo_trades(fills)
    if not trades:
        return [], executed
    attribute = await order_attributor(session, [orders[t.order_id] for t in trades])
    cache: dict[uuid.UUID, str | None] = {}
    for t in trades:
        if t.order_id not in cache:
            cache[t.order_id] = await attribute(orders[t.order_id])
        t.strategy = cache[t.order_id]
    states = await strategy_states(session)
    rates = _Rates(qs)
    groups = defaultdict(list)
    for t in trades:
        groups[t.strategy].append(t)
    out = []
    for ref, items in groups.items():
        opened = [t for t in items if since is None or t.opened_at >= since]
        closed = [t for t in items if t.closed_at is not None
                  and (since is None or t.closed_at >= since)]
        open_pos = {(t.portfolio_id, t.symbol) for t in items if t.open_qty}
        if not (opened or closed or open_pos):
            continue
        reals, costs, wins, known = [], [], 0, True
        for t in closed:
            real = await rates.eur(t.realized, t.currency)
            cost = _sum([await rates.eur(a, c) for c, a in sorted(t.costs.items())])
            reals.append(real)
            costs.append(cost)
            if real is None or cost is None:
                known = False
            elif real - cost > 0:
                wins += 1
        real_sum, cost_sum = _sum(reals), _sum(costs)
        held = [(t.closed_at - t.opened_at).total_seconds() for t in closed]
        state = states.get(ref) if ref else None
        out.append(StrategyResult(
            ref=ref, name=state.name if state else None,
            status=state.status if state else None,
            portfolios=sorted({portfolios[t.portfolio_id].name for t in items}),
            trades_opened=len(opened), trades_closed=len(closed), wins=wins,
            win_rate_pct=_pct(wins, len(closed)) if known else None,
            realized_pnl_eur=_money(real_sum), costs_eur=_money(cost_sum),
            net_pnl_eur=_money(None if None in (real_sum, cost_sum) else real_sum - cost_sum),
            avg_holding_s=round(sum(held) / len(held)) if held else None,
            open_positions=len(open_pos),
        ))
    out.sort(key=lambda s: (s.ref is None, int(s.ref[4:]) if s.ref else 0))
    return out, executed


async def agent_results(session: AsyncSession, qs: QuoteSource, period: str,
                        *, now: datetime | None = None) -> AgentResults:
    now = now or datetime.now(UTC)
    tz = get_settings().tz
    first, since = period_bounds(period, now, tz)
    today = now.astimezone(ZoneInfo(tz)).date()

    agents = sorted(await session.scalars(select(Agent)), key=lambda a: a.name.casefold())
    rows = {a.id: AgentResult(agent_id=a.id, name=a.name, active=a.active,
                              decisions={k.value: 0 for k in DecisionKind}) for a in agents}

    # decisions of the period
    q = select(Decision).order_by(Decision.id)
    if since is not None:
        q = q.where(Decision.created_at >= since)
    decisions = list(await session.scalars(q))
    outcomes = _outcomes(decisions)
    aliases = {a.id: agent_aliases(a.name) for a in agents}
    lessons = repeated = 0
    for d in decisions:
        r = rows.get(d.agent_id)
        if r is not None:
            r.decisions[d.kind] = r.decisions.get(d.kind, 0) + 1
            r.decisions_total += 1
            if d.kind == "proposal":
                r.proposals += 1
                setattr(r, outcomes[d.id], getattr(r, outcomes[d.id]) + 1)
            elif d.kind == "lesson":
                r.lessons_written += 1
        if d.kind == "lesson":
            lessons += 1
            rep = bool(_details(d).get("repeats"))
            repeated += rep
            named = _named(d)
            for aid, names in aliases.items():
                if named & names:
                    rows[aid].lessons_about += 1
                    rows[aid].lessons_repeated += rep
    kinds = Counter(d.kind for d in decisions)
    verdicts = Counter(outcomes.values())

    # MCP calls
    mq = (select(McpAuditLog.agent_id, func.count(),
                 func.count(case((McpAuditLog.outcome != "ok", 1))))
          .where(McpAuditLog.agent_id.is_not(None))
          .group_by(McpAuditLog.agent_id))
    if since is not None:
        mq = mq.where(McpAuditLog.created_at >= since)
    for aid, calls, errors in (await session.execute(mq)).all():
        if (r := rows.get(aid)) is not None:
            r.mcp_calls, r.mcp_errors = calls, errors
            r.mcp_error_pct = _pct(errors, calls)

    # costs (Paperclip reader)
    cfg = await load_config(session)
    state = await load_state(session)
    cq = (select(AgentCostDay.paperclip_agent_id, func.max(AgentCostDay.paperclip_agent_name),
                 func.sum(AgentCostDay.api_usd), func.sum(AgentCostDay.subscription_usd),
                 func.sum(AgentCostDay.other_usd), func.sum(AgentCostDay.runs))
          .where(AgentCostDay.day <= today)
          .group_by(AgentCostDay.paperclip_agent_id))
    if first is not None:
        cq = cq.where(AgentCostDay.day >= first)
    smt = await smt_agents(session)
    cost = billed = unmatched = Decimal(0)
    for pid, name, api, sub, other, runs in (await session.execute(cq)).all():
        api, sub, other = Decimal(api or 0), Decimal(sub or 0), Decimal(other or 0)
        cost += api + sub + other
        billed += api + other
        m = match_agent(pid, name, smt, cfg.agent_map)
        if m is None or m.id not in rows:
            unmatched += api + sub + other
            continue
        r = rows[m.id]
        r.cost_usd += api + sub + other
        r.billed_usd += api + other
        r.runs += int(runs or 0)
        if name and name not in r.paperclip_names:
            r.paperclip_names.append(name)

    # strategies and executed orders
    strategies, executed = await _strategies(session, qs, since)
    per_agent = Counter(executed.values())
    for r in rows.values():
        r.orders_executed = per_agent.get(str(r.agent_id), 0)
        r.cost_per_order_usd = _ratio(r.cost_usd, r.orders_executed, _USD)
        r.approval_rate_pct = _pct(r.approved, r.approved + r.vetoed)
        r.paperclip_names.sort()

    pnl = _sum(s.net_pnl_eur for s in strategies) if strategies else Decimal(0)
    fees = _sum(s.costs_eur for s in strategies) if strategies else Decimal(0)
    team = TeamResult(
        pnl_eur=_money(pnl), costs_eur=_money(fees), orders_executed=len(executed),
        proposals=kinds["proposal"], approvals=kinds["approval"], vetoes=kinds["veto"],
        approval_rate_pct=_pct(verdicts["approved"], verdicts["approved"] + verdicts["vetoed"]),
        lessons=lessons, lessons_repeated=repeated,
        rules_active=await _rules_active(session),
        cost_usd=cost.quantize(_USD, ROUND_HALF_UP),
        billed_usd=billed.quantize(_USD, ROUND_HALF_UP),
        unmatched_cost_usd=unmatched.quantize(_USD, ROUND_HALF_UP),
        pnl_per_usd=_ratio(pnl, cost, _USD),
        cost_per_order_usd=_ratio(cost, len(executed), _USD),
    )
    for r in rows.values():
        r.cost_usd = r.cost_usd.quantize(_USD, ROUND_HALF_UP)
        r.billed_usd = r.billed_usd.quantize(_USD, ROUND_HALF_UP)
    return AgentResults(period=period, from_=first, to=today,
                        last_read_at=state.get("last_ok_at"), team=team,
                        agents=list(rows.values()), strategies=strategies)
